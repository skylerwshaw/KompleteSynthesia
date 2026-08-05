//
//  ODRClient.m
//  KompleteSynthesia
//

#import "ODRClient.h"

#import <sys/socket.h>
#import <sys/un.h>
#import <unistd.h>

#import "LogViewController.h"

static NSString* const kODRSocketPath = @"/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks";

// `instance_hello` is the only method addressed by name. Everything after it uses integer
// symbols from the service's registry, these are what agent 2.0.7 (R15) / IPC protocol
// 2.1.0 was observed using, and they are not guaranteed stable across agent releases.
static const uint32_t kODRMethodConnectDevice = 382;
static const uint32_t kODRMethodRequestFocus = 373;
static const uint32_t kODRMethodLightGuide = 360;
static const uint32_t kODRFieldLeds = 239;

static NSString* const kODRProtocolVersion = @"2.1.0";

// The LED array is always the full MIDI range, whatever the keyboard's key count.
static const NSUInteger kODRLedCount = 128;

static const NSTimeInterval kODRReplyTimeout = 2.0;

// Minimal MessagePack encoding, same approach as MK3Protocol.m but for the types this
// protocol needs: the 36-character client UUID needs str8, and the 128-entry LED array
// needs array16, neither of which fits that file's fixstr/fixarray helpers.

static void ODRAppendUInt(NSMutableData* d, uint32_t value)
{
    if (value <= 0x7f) {
        uint8_t b = (uint8_t)value;
        [d appendBytes:&b length:1];
    } else if (value <= 0xff) {
        uint8_t bytes[2] = {0xcc, (uint8_t)value};
        [d appendBytes:bytes length:2];
    } else if (value <= 0xffff) {
        uint8_t prefix = 0xcd;
        [d appendBytes:&prefix length:1];
        uint16_t v = CFSwapInt16HostToBig((uint16_t)value);
        [d appendBytes:&v length:2];
    } else {
        uint8_t prefix = 0xce;
        [d appendBytes:&prefix length:1];
        uint32_t v = CFSwapInt32HostToBig(value);
        [d appendBytes:&v length:4];
    }
}

static void ODRAppendString(NSMutableData* d, NSString* s)
{
    NSData* utf8 = [s dataUsingEncoding:NSUTF8StringEncoding];
    NSUInteger len = utf8.length;
    assert(len <= 0xff);
    if (len <= 31) {
        uint8_t prefix = 0xa0 | (uint8_t)len;
        [d appendBytes:&prefix length:1];
    } else {
        uint8_t bytes[2] = {0xd9, (uint8_t)len};
        [d appendBytes:bytes length:2];
    }
    [d appendData:utf8];
}

static void ODRAppendMap(NSMutableData* d, NSUInteger count)
{
    assert(count <= 15);
    uint8_t b = 0x80 | (uint8_t)count;
    [d appendBytes:&b length:1];
}

static void ODRAppendArray(NSMutableData* d, NSUInteger count)
{
    if (count <= 15) {
        uint8_t b = 0x90 | (uint8_t)count;
        [d appendBytes:&b length:1];
        return;
    }
    assert(count <= 0xffff);
    uint8_t prefix = 0xdc;
    [d appendBytes:&prefix length:1];
    uint16_t v = CFSwapInt16HostToBig((uint16_t)count);
    [d appendBytes:&v length:2];
}

@implementation ODRClient {
    LogViewController* log;

    int sock;
    NSString* uuid;
    NSString* serial;
    uint32_t msgid;

    dispatch_source_t drain;
}

+ (BOOL)serviceAvailable
{
    return [[NSFileManager defaultManager] fileExistsAtPath:kODRSocketPath];
}

- (instancetype)initWithLogViewController:(LogViewController*)logViewController
{
    self = [super init];
    if (self) {
        log = logViewController;
        sock = -1;
    }
    return self;
}

- (void)dealloc
{
    [self disconnect];
}

- (BOOL)isConnected
{
    return sock >= 0 && serial != nil;
}

#pragma mark - Framing

// Every message is a uint32 little-endian length followed by an msgpack-RPC message.
- (BOOL)sendMessage:(NSData*)message
{
    if (sock < 0) {
        return NO;
    }

    NSMutableData* packet = [NSMutableData dataWithCapacity:message.length + 4];
    uint32_t lengthLE = CFSwapInt32HostToLittle((uint32_t)message.length);
    [packet appendBytes:&lengthLE length:4];
    [packet appendData:message];

    const uint8_t* bytes = packet.bytes;
    size_t remaining = packet.length;
    while (remaining > 0) {
        ssize_t written = send(sock, bytes, remaining, 0);
        if (written <= 0) {
            if (errno == EINTR) {
                continue;
            }
            [log logLine:[NSString stringWithFormat:@"ODR write failed: %s", strerror(errno)]];
            [self disconnect];
            return NO;
        }
        bytes += written;
        remaining -= written;
    }
    return YES;
}

// Reads one whole frame, keeping only its tail. Enough to tell a success from a refusal
// without a decoder: a reply is [1, msgid, error, result], so an accepted call ends in
// nil-then-true while a refusal carries an error string in place of that nil.
- (BOOL)replyWasAccepted:(NSData*)reply
{
    if (reply.length < 2) {
        return NO;
    }
    const uint8_t* tail = (const uint8_t*)reply.bytes + reply.length - 2;
    return tail[0] == 0xc0 && tail[1] == 0xc3;
}

- (NSData*)awaitReply
{
    uint8_t header[4];
    size_t got = 0;
    while (got < sizeof(header)) {
        ssize_t n = recv(sock, header + got, sizeof(header) - got, 0);
        if (n <= 0) {
            return nil;
        }
        got += n;
    }

    uint32_t length;
    memcpy(&length, header, sizeof(length));
    uint32_t size = CFSwapInt32LittleToHost(length);

    // The hello reply runs to ten kilobytes of device and asset inventory we have no use
    // for; only the last handful of bytes ever gets looked at.
    uint8_t scratch[4096];
    NSMutableData* tail = [NSMutableData data];
    while (size > 0) {
        ssize_t n = recv(sock, scratch, MIN(size, sizeof(scratch)), 0);
        if (n <= 0) {
            return nil;
        }
        [tail setData:[NSData dataWithBytes:scratch length:n]];
        size -= n;
    }
    return tail;
}

- (NSData*)requestWithMethod:(NSData*)method params:(NSData*)params
{
    NSMutableData* message = [NSMutableData data];
    ODRAppendArray(message, 4);
    ODRAppendUInt(message, 0);
    ODRAppendUInt(message, msgid++);
    [message appendData:method];
    [message appendData:params];
    return message;
}

- (NSData*)notificationWithMethod:(uint32_t)method params:(NSData*)params
{
    NSMutableData* message = [NSMutableData data];
    ODRAppendArray(message, 3);
    ODRAppendUInt(message, 2);
    ODRAppendUInt(message, method);
    [message appendData:params];
    return message;
}

#pragma mark - Session

- (BOOL)failWithError:(NSError**)error message:(NSString*)message
{
    [log logLine:[NSString stringWithFormat:@"ODR: %@", message]];
    if (error != nil) {
        *error = [NSError errorWithDomain:[[NSBundle bundleForClass:[self class]] bundleIdentifier]
                                     code:-1
                                 userInfo:@{
                                     NSLocalizedDescriptionKey : message,
                                     NSLocalizedRecoverySuggestionErrorKey :
                                         @"Native Instruments' hardware connection service may not be running."
                                 }];
    }
    [self disconnect];
    return NO;
}

- (BOOL)connectToDeviceWithSerial:(NSString*)deviceSerial error:(NSError**)error
{
    [self disconnect];

    sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock < 0) {
        return [self failWithError:error message:@"could not create a socket"];
    }

    // Without this a service that goes away takes the whole app down with SIGPIPE.
    int on = 1;
    setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof(on));

    struct timeval timeout = {.tv_sec = (time_t)kODRReplyTimeout, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

    struct sockaddr_un addr = {0};
    addr.sun_family = AF_UNIX;
    const char* path = kODRSocketPath.fileSystemRepresentation;
    if (strlen(path) >= sizeof(addr.sun_path)) {
        return [self failWithError:error message:@"socket path is too long"];
    }
    strlcpy(addr.sun_path, path, sizeof(addr.sun_path));

    if (connect(sock, (struct sockaddr*)&addr, (socklen_t)SUN_LEN(&addr)) != 0) {
        return [self failWithError:error
                           message:[NSString stringWithFormat:@"could not reach the service: %s", strerror(errno)]];
    }

    NSUUID* identity = [NSUUID UUID];
    uuid = identity.UUIDString.lowercaseString;
    msgid = 0;

    // The connection opens with the client UUID as 16 raw bytes, ahead of and outside the
    // length-prefixed framing. Miss it and the service reads everything and answers
    // nothing at all, no error, no close.
    uuid_t raw;
    [identity getUUIDBytes:raw];
    if (send(sock, raw, sizeof(raw), 0) != sizeof(raw)) {
        return [self failWithError:error message:@"could not send the client identity"];
    }

    NSMutableData* method = [NSMutableData data];
    ODRAppendString(method, @"instance_hello");

    NSMutableData* params = [NSMutableData data];
    ODRAppendArray(params, 2);
    ODRAppendString(params, uuid);
    ODRAppendMap(params, 2);
    ODRAppendString(params, @"client_info");
    ODRAppendMap(params, 3);
    ODRAppendString(params, @"name");
    ODRAppendString(params, @"KompleteSynthesia");
    ODRAppendString(params, @"version");
    ODRAppendString(params, [[[NSBundle mainBundle] infoDictionary] objectForKey:@"CFBundleShortVersionString"]
                                ?: @"1.0.0");
    ODRAppendString(params, @"type");
    ODRAppendString(params, @"standalone");
    ODRAppendString(params, @"ipc_protocol_version");
    ODRAppendString(params, kODRProtocolVersion);

    if ([self sendMessage:[self requestWithMethod:method params:params]] == NO) {
        return [self failWithError:error message:@"handshake could not be sent"];
    }
    if ([self awaitReply] == nil) {
        return [self failWithError:error message:@"no answer to the handshake"];
    }

    NSMutableData* attachMethod = [NSMutableData data];
    ODRAppendUInt(attachMethod, kODRMethodConnectDevice);

    NSMutableData* attachParams = [NSMutableData data];
    ODRAppendArray(attachParams, 2);
    ODRAppendString(attachParams, uuid);
    ODRAppendString(attachParams, deviceSerial);

    if ([self sendMessage:[self requestWithMethod:attachMethod params:attachParams]] == NO) {
        return [self failWithError:error message:@"could not attach to the keyboard"];
    }
    NSData* attachReply = [self awaitReply];
    if (attachReply == nil) {
        return [self failWithError:error message:@"no answer when attaching to the keyboard"];
    }
    // Checked rather than assumed: a refusal here (unknown serial, or a renumbered method
    // in some future agent) would otherwise go unnoticed until the keys quietly stayed
    // dark, having already disabled the legacy path that would have lit them.
    if ([self replyWasAccepted:attachReply] == NO) {
        return [self failWithError:error
                           message:[NSString stringWithFormat:@"service refused the keyboard %@", deviceSerial]];
    }

    serial = deviceSerial;

    // Lighting is accepted and silently discarded unless the client holds focus.
    NSMutableData* focusParams = [NSMutableData data];
    ODRAppendArray(focusParams, 2);
    ODRAppendString(focusParams, uuid);
    ODRAppendString(focusParams, serial);
    if ([self sendMessage:[self notificationWithMethod:kODRMethodRequestFocus params:focusParams]] == NO) {
        return [self failWithError:error message:@"could not take focus"];
    }

    [self startDraining];

    [log logLine:[NSString stringWithFormat:@"ODR lighting active for %@", serial]];
    return YES;
}

// The service keeps sending notifications we have no use for. Nothing reads them, so
// without this the socket buffer fills up and our writes eventually block.
- (void)startDraining
{
    int fd = sock;
    dispatch_source_t source = dispatch_source_create(DISPATCH_SOURCE_TYPE_READ, fd, 0,
                                                      dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_BACKGROUND, 0));
    if (source == nil) {
        return;
    }

    dispatch_source_set_event_handler(source, ^{
      uint8_t scratch[4096];
      ssize_t n = recv(fd, scratch, sizeof(scratch), MSG_DONTWAIT);
      // At EOF the socket stays readable forever, so stop rather than spin.
      if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
          dispatch_source_cancel(source);
      }
    });
    // The source owns the descriptor from here on, closing it out from under a live
    // source risks the read landing on whatever gets that number next.
    dispatch_source_set_cancel_handler(source, ^{
      close(fd);
    });

    drain = source;
    dispatch_resume(drain);
}

- (void)disconnect
{
    if (drain != nil) {
        dispatch_source_cancel(drain);
        drain = nil;
    } else if (sock >= 0) {
        close(sock);
    }
    sock = -1;
    serial = nil;
    uuid = nil;
}

#pragma mark - Lighting

- (BOOL)setKeyColors:(const unsigned char*)colors count:(size_t)count firstNote:(int)firstNote
{
    if (self.isConnected == NO || colors == NULL) {
        return NO;
    }

    NSMutableData* leds = [NSMutableData dataWithCapacity:kODRLedCount * 3];
    ODRAppendArray(leds, kODRLedCount);
    for (NSUInteger note = 0; note < kODRLedCount; note++) {
        NSInteger key = (NSInteger)note - firstNote;
        ODRAppendArray(leds, 2);
        ODRAppendUInt(leds, (uint32_t)note);
        ODRAppendUInt(leds, (key >= 0 && key < (NSInteger)count) ? colors[key] : 0);
    }

    NSMutableData* params = [NSMutableData data];
    ODRAppendArray(params, 3);
    ODRAppendString(params, uuid);
    ODRAppendString(params, serial);
    ODRAppendMap(params, 1);
    ODRAppendUInt(params, kODRFieldLeds);
    [params appendData:leds];

    return [self sendMessage:[self notificationWithMethod:kODRMethodLightGuide params:params]];
}

@end
