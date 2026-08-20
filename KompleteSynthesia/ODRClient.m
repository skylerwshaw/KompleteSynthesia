//
//  ODRClient.m
//  KompleteSynthesia
//

#import "ODRClient.h"

#import <sys/socket.h>
#import <sys/un.h>
#import <unistd.h>

#import <CommonCrypto/CommonDigest.h>

#import "LogViewController.h"

static NSString* const kODRSocketPath = @"/Users/Shared/Native Instruments/com.native-instruments.odr_agent.kks";

// `instance_hello` is the only method addressed by name. Everything after it uses integer
// symbols, and those numbers are not fixed, confirmed drifting between agent releases
// (issue #18, comment 5208665711: 382/373/360 all came back "Method not registered" on
// agent 2.1.5 after working on 2.0.7). What is fixed is the *name*: the hello reply
// includes the service's whole symbol_registry array, and a method's number is simply its
// index in that array (confirmed against real Komplete Kontrol traffic through a relay;
// see ODR_PROTOCOL.md). So these are names, resolved fresh every connection via
// ODRFindSymbolRegistry below, not baked-in numbers that go stale again next agent update.
static NSString* const kODRSymbolConnectDevice = @"connect_device";
static NSString* const kODRSymbolRequestFocus = @"client_request_focus";
static NSString* const kODRSymbolLightGuide = @"client_lightguide_set_leds";
static NSString* const kODRSymbolMidiAddressing = @"client_midi_addressing";
static NSString* const kODRSymbolAddAsset = @"add_asset";

// Sentinel for a name the connected agent's registry did not provide. Screen support
// (add_asset) is optional: an older agent simply lacks it, and lighting must still work.
static const uint32_t kODRMethodUnresolved = UINT32_MAX;

// Agent 2.0.7 (R15) / IPC protocol 2.1.0 (the generation this app was first confirmed
// against) is not known to include a symbol_registry in its hello reply at all (nobody
// needed one before the numbers moved). If a service's hello reply has no registry, these
// are what that generation was confirmed using, kept as a fallback so an un-updated
// service still lights the keyboard rather than being told the protocol doesn't work.
static const uint32_t kODRLegacyMethodConnectDevice = 382;
static const uint32_t kODRLegacyMethodRequestFocus = 373;
static const uint32_t kODRLegacyMethodLightGuide = 360;
static const uint32_t kODRLegacyFieldMidiAddressing = 239;

static NSString* const kODRProtocolVersion = @"2.1.0";

// The LED array is always the full MIDI range, whatever the keyboard's key count.
static const NSUInteger kODRLedCount = 128;

static const NSTimeInterval kODRReplyTimeout = 2.0;

// Minimal MessagePack encoding for the types this protocol needs: the 36-character
// client UUID needs str8, and the 128-entry LED array needs array16.

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

// msgpack `bin`: a raw byte string, used for the 32-byte asset handle and the image bytes.
// Distinct from `str`; the service's asset store rejects anything that is not a bin.
static void ODRAppendBinary(NSMutableData* d, const void* bytes, NSUInteger len)
{
    if (len <= 0xff) {
        uint8_t header[2] = {0xc4, (uint8_t)len};
        [d appendBytes:header length:2];
    } else if (len <= 0xffff) {
        uint8_t prefix = 0xc5;
        [d appendBytes:&prefix length:1];
        uint16_t v = CFSwapInt16HostToBig((uint16_t)len);
        [d appendBytes:&v length:2];
    } else {
        uint8_t prefix = 0xc6;
        [d appendBytes:&prefix length:1];
        uint32_t v = CFSwapInt32HostToBig((uint32_t)len);
        [d appendBytes:&v length:4];
    }
    [d appendBytes:bytes length:len];
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

// Finds "symbol_registry"'s value in a hello reply and decodes it as an array of strings,
// the only structured data this app ever reads back, out of a reply that otherwise runs to
// ten kilobytes of device/asset inventory it has no use for. A byte-pattern search for the
// key plus a decoder scoped to exactly "array of strings" is a smaller, easier-to-trust diff
// than a general msgpack decoder, and it is all this app needs: the key is always encoded as
// fixstr (0xaf, "symbol_registry" is always exactly 15 bytes), so it cannot be confused with
// anything except that literal string appearing inside binary asset data, which does not
// happen in practice.
static NSArray<NSString*>* ODRFindSymbolRegistry(NSData* reply)
{
    static const uint8_t key[] = {0xaf, 's', 'y', 'm', 'b', 'o', 'l', '_',
                                   'r', 'e', 'g', 'i', 's', 't', 'r', 'y'};
    const uint8_t* bytes = reply.bytes;
    NSUInteger length = reply.length;
    if (length < sizeof(key)) {
        return nil;
    }

    NSUInteger cursor = NSNotFound;
    for (NSUInteger i = 0; i + sizeof(key) <= length; i++) {
        if (memcmp(bytes + i, key, sizeof(key)) == 0) {
            cursor = i + sizeof(key);
            break;
        }
    }
    if (cursor == NSNotFound || cursor >= length) {
        return nil;
    }

    uint8_t arrayTag = bytes[cursor++];
    NSUInteger count;
    if ((arrayTag & 0xf0) == 0x90) {
        count = arrayTag & 0x0f;
    } else if (arrayTag == 0xdc) {
        if (cursor + 2 > length) return nil;
        count = ((NSUInteger)bytes[cursor] << 8) | bytes[cursor + 1];
        cursor += 2;
    } else if (arrayTag == 0xdd) {
        if (cursor + 4 > length) return nil;
        count = ((NSUInteger)bytes[cursor] << 24) | ((NSUInteger)bytes[cursor + 1] << 16) |
                ((NSUInteger)bytes[cursor + 2] << 8) | bytes[cursor + 3];
        cursor += 4;
    } else {
        return nil;
    }

    NSMutableArray<NSString*>* names = [NSMutableArray arrayWithCapacity:count];
    for (NSUInteger i = 0; i < count; i++) {
        if (cursor >= length) return nil;
        uint8_t strTag = bytes[cursor++];
        NSUInteger strLen;
        if ((strTag & 0xe0) == 0xa0) {
            strLen = strTag & 0x1f;
        } else if (strTag == 0xd9) {
            if (cursor >= length) return nil;
            strLen = bytes[cursor++];
        } else if (strTag == 0xda) {
            if (cursor + 2 > length) return nil;
            strLen = ((NSUInteger)bytes[cursor] << 8) | bytes[cursor + 1];
            cursor += 2;
        } else {
            return nil;  // registry entries are always names, an unexpected type means
                          // this decoder found the wrong bytes, not a real registry.
        }
        if (cursor + strLen > length) return nil;
        NSString* name = [[NSString alloc] initWithBytes:bytes + cursor
                                                    length:strLen
                                                  encoding:NSUTF8StringEncoding];
        if (name == nil) return nil;
        [names addObject:name];
        cursor += strLen;
    }
    return names;
}

@implementation ODRClient {
    LogViewController* log;

    int sock;
    NSString* uuid;
    NSString* serial;
    uint32_t msgid;

    // Resolved from the service's symbol_registry at connect time, see
    // ODRFindSymbolRegistry and the kODRSymbol* names above. setKeyColors: needs these
    // after connectToDeviceWithSerial: has returned, so they outlive that one call.
    uint32_t lightGuideMethod;
    uint32_t midiAddressingField;
    uint32_t addAssetMethod;

    dispatch_source_t drain;
}

+ (BOOL)serviceAvailable
{
    return [[NSFileManager defaultManager] fileExistsAtPath:kODRSocketPath];
}

+ (BOOL)runEncodingSelfTest
{
    // bin8 framing: one byte -> 0xc4, len, payload.
    NSMutableData* d = [NSMutableData data];
    uint8_t one = 0x41;
    ODRAppendBinary(d, &one, 1);
    const uint8_t expect8[] = {0xc4, 0x01, 0x41};
    if (d.length != sizeof(expect8) || memcmp(d.bytes, expect8, sizeof(expect8)) != 0) {
        NSLog(@"selftest FAIL: bin8 framing");
        return NO;
    }

    // bin16 framing at 256 bytes: 0xc5, big-endian uint16 length.
    d = [NSMutableData data];
    NSMutableData* mid = [NSMutableData dataWithLength:256];
    ODRAppendBinary(d, mid.bytes, mid.length);
    const uint8_t* b = d.bytes;
    if (d.length != 3 + 256 || b[0] != 0xc5 || b[1] != 0x01 || b[2] != 0x00) {
        NSLog(@"selftest FAIL: bin16 framing");
        return NO;
    }

    // bin32 framing at 65536 bytes: 0xc6, big-endian uint32 length.
    d = [NSMutableData data];
    NSMutableData* big = [NSMutableData dataWithLength:0x10000];
    ODRAppendBinary(d, big.bytes, big.length);
    b = d.bytes;
    if (d.length != 5 + 0x10000 || b[0] != 0xc6 || b[1] != 0x00 || b[2] != 0x01 || b[3] != 0x00 ||
        b[4] != 0x00) {
        NSLog(@"selftest FAIL: bin32 framing");
        return NO;
    }

    // SHA-256("abc") known-answer vector.
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256("abc", 3, digest);
    const uint8_t expected[] = {0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40,
                                0xde, 0x5d, 0xae, 0x22, 0x23, 0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17,
                                0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad};
    if (memcmp(digest, expected, sizeof(expected)) != 0) {
        NSLog(@"selftest FAIL: sha256 vector");
        return NO;
    }

    // add_asset params shape: fixarray-2, then a 32-byte handle bin, then the image bin.
    NSData* image = [@"fake-image-bytes" dataUsingEncoding:NSUTF8StringEncoding];
    NSMutableData* params = [NSMutableData data];
    ODRAppendArray(params, 2);
    ODRAppendBinary(params, digest, sizeof(digest));
    ODRAppendBinary(params, image.bytes, image.length);
    b = params.bytes;
    if (b[0] != 0x92 || b[1] != 0xc4 || b[2] != 0x20 ||
        params.length != 1 + 2 + 32 + 2 + image.length) {
        NSLog(@"selftest FAIL: add_asset params shape");
        return NO;
    }

    NSLog(@"selftest OK: ODR bin encoding + 32-byte sha256 asset handle");
    return YES;
}

- (instancetype)initWithLogViewController:(LogViewController*)logViewController
{
    self = [super init];
    if (self) {
        log = logViewController;
        sock = -1;
        addAssetMethod = kODRMethodUnresolved;
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

    // The hello reply runs to ten kilobytes of device and asset inventory, most of which
    // goes unused, but ODRFindSymbolRegistry needs the whole body: symbol_registry is
    // the last key in it, so a reply that only kept the final recv() chunk (as this used
    // to, back when replyWasAccepted:'s last-2-bytes check was the only reader) would
    // usually cut it off mid-array. Every chunk has to be kept, not just the last one.
    uint8_t scratch[4096];
    NSMutableData* body = [NSMutableData dataWithCapacity:size];
    while (size > 0) {
        ssize_t n = recv(sock, scratch, MIN(size, sizeof(scratch)), 0);
        if (n <= 0) {
            return nil;
        }
        [body appendBytes:scratch length:n];
        size -= n;
    }
    return body;
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
    NSData* helloReply = [self awaitReply];
    if (helloReply == nil) {
        return [self failWithError:error message:@"no answer to the handshake"];
    }

    uint32_t connectDeviceMethod;
    uint32_t requestFocusMethod;
    NSArray<NSString*>* registry = ODRFindSymbolRegistry(helloReply);
    if (registry == nil) {
        // No registry at all, an older agent, as far as is known (see the kODRLegacy*
        // comment above). Not a failure: fall back to the numbers that generation was
        // confirmed using, rather than refuse to light a keyboard whose service just
        // predates having a registry to resolve names from.
        [log logLine:@"ODR: hello reply has no symbol registry, using legacy method numbers"];
        connectDeviceMethod = kODRLegacyMethodConnectDevice;
        requestFocusMethod = kODRLegacyMethodRequestFocus;
        lightGuideMethod = kODRLegacyMethodLightGuide;
        midiAddressingField = kODRLegacyFieldMidiAddressing;
        // Screen support needs a registry to resolve add_asset from; a legacy agent has
        // none, so it lights keys but cannot drive the screen.
        addAssetMethod = kODRMethodUnresolved;
    } else {
        NSUInteger iConnect = [registry indexOfObject:kODRSymbolConnectDevice];
        NSUInteger iFocus = [registry indexOfObject:kODRSymbolRequestFocus];
        NSUInteger iLeds = [registry indexOfObject:kODRSymbolLightGuide];
        NSUInteger iAddr = [registry indexOfObject:kODRSymbolMidiAddressing];
        if (iConnect == NSNotFound || iFocus == NSNotFound || iLeds == NSNotFound || iAddr == NSNotFound) {
            return [self failWithError:error
                               message:@"service's symbol registry is missing a method this app needs"];
        }
        connectDeviceMethod = (uint32_t)iConnect;
        requestFocusMethod = (uint32_t)iFocus;
        lightGuideMethod = (uint32_t)iLeds;
        midiAddressingField = (uint32_t)iAddr;

        // Screen support is optional and resolved best-effort: a missing add_asset just
        // means this agent cannot drive the screen, never a failed connection, because
        // lighting is the core feature and must work regardless.
        NSUInteger iAsset = [registry indexOfObject:kODRSymbolAddAsset];
        addAssetMethod = (iAsset == NSNotFound) ? kODRMethodUnresolved : (uint32_t)iAsset;
    }

    NSMutableData* attachMethod = [NSMutableData data];
    ODRAppendUInt(attachMethod, connectDeviceMethod);

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
    // Checked rather than assumed: a refusal here (unknown serial, or a name missing from
    // this agent's registry) would otherwise go unnoticed until the keys quietly stayed
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
    if ([self sendMessage:[self notificationWithMethod:requestFocusMethod params:focusParams]] == NO) {
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
    ODRAppendUInt(params, midiAddressingField);
    [params appendData:leds];

    return [self sendMessage:[self notificationWithMethod:lightGuideMethod params:params]];
}

#pragma mark - Screen

- (BOOL)screenSupported
{
    return self.isConnected && addAssetMethod != kODRMethodUnresolved;
}

- (NSData*)uploadImageAsset:(NSData*)imageBytes
{
    if (self.screenSupported == NO || imageBytes.length == 0) {
        return nil;
    }

    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(imageBytes.bytes, (CC_LONG)imageBytes.length, digest);

    // The handle must be exactly 32 bytes. A wrong-length handle deserialises into the
    // service's strong sha256 type, throws an uncaught C++ exception, and takes the whole
    // NIHardwareConnectionService (and thus the light guide) down with it. CC_SHA256 always
    // yields 32, but the invariant is load-bearing, so it is asserted here rather than
    // assumed. See ODR_PROTOCOL.md / MK3_VIDEO_RESEARCH.md and docs/adr/0001.
    if (CC_SHA256_DIGEST_LENGTH != 32) {
        return nil;
    }

    // `add_asset` is a notification (it has no reply): [ <32-byte handle bin>, <image bin> ].
    NSMutableData* params = [NSMutableData data];
    ODRAppendArray(params, 2);
    ODRAppendBinary(params, digest, CC_SHA256_DIGEST_LENGTH);
    ODRAppendBinary(params, imageBytes.bytes, imageBytes.length);

    if ([self sendMessage:[self notificationWithMethod:addAssetMethod params:params]] == NO) {
        return nil;
    }
    return [NSData dataWithBytes:digest length:CC_SHA256_DIGEST_LENGTH];
}

@end
