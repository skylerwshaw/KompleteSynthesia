//
//  MIDIController.m
//  KompleteSynthesia
//
//  Created by Till Toenshoff on 06.01.23.
//

#import "MIDIController.h"

#import <CoreMIDI/CoreMIDI.h>

#import "LogViewController.h"

NSString* kMIDIInputInterfaceLightLoopback = @"LoopBe";
NSString* kMIDIInputInterfaceKeyboard = @"Port 1";
// MK3 controllers name their note-carrying MIDI port "Main" instead of "Port 1".
NSString* kMIDIInputInterfaceKeyboardMK3 = @"Main";
// MK3 controllers expose a "DAW" port carrying buttons/jogwheel as Mackie-Control-style
// MIDI, since those don't show up on the vendor HID interface at all. It stays silent
// until we send it the NIHIA "hello" handshake below, reverse-engineered from the
// open-source DrivenByMoss Bitwig extension (git-moss/DrivenByMoss,
// KontrolProtocolControlSurface.java), which documents this as a real, if undocumented
// by NI, MIDI CC protocol.
NSString* kMIDIInputInterfaceControlSurfaceMK3 = @"DAW";
static const UInt8 kMIDIControlSurfaceHandshakeCC = 0x01;
static const UInt8 kMIDIControlSurfaceGoodbyeCC = 0x02;
static const UInt8 kMIDIControlSurfaceProtocolVersion = 4; // MK3 uses NIHIA protocol v4.
static const UInt8 kMIDIControlSurfaceChannel = 0x0F;      // Channel 16, zero-indexed.

// Once the device acknowledges CC_HELLO (echoes it back with the agreed protocol
// version), the host is expected to identify itself back via this SysEx "DAW info"
// message or the device won't proceed past the handshake.
static const UInt8 kMIDIControlSurfaceSysExHeader[] = {0xF0, 0x00, 0x21, 0x09, 0x00, 0x00, 0x44, 0x43, 0x01, 0x00};
static const UInt8 kMIDIControlSurfaceSysExIdentity = 0x07;
// The reference implementation notes this "needs to be sent all the time to activate the
// PLUG-IN button", i.e. some device functionality appears gated on receiving DAW state
// like tempo, not just the identity handshake.
static const UInt8 kMIDIControlSurfaceSysExSetTempo = 0x19;
static const double kMIDIControlSurfaceTenNsPerMinute = 6.0e9;

/// Listens on the "IAC Driver LoopBe" and the "Komplete Kontrol Sx MKx Port 1" interfaces and forwards
/// note on/off events as well as control change events to its delegate.

@implementation MIDIController {
    MIDIClientRef client;

    MIDIPortRef portKeyboard;
    MIDIPortRef portLight;
    MIDIPortRef portControlSurface;
    MIDIPortRef outputPortControlSurface;
    MIDIEndpointRef controlSurfaceDestination;
    BOOL sentControlSurfaceHandshake;

    BOOL connected;

    LogViewController* log;
}

+ (NSString*)readableNote:(unsigned char)note
{
    int octave = ((int)note / 12) - 1;
    NSArray* noteNames = @[ @"C", @"C#", @"D", @"D#", @"E", @"F", @"F#", @"G", @"G#", @"A", @"A#", @"B" ];
    NSString* readable = [NSString stringWithFormat:@"%@%d", noteNames[note % 12], octave];
    NSString* output = @"   ";
    return [output stringByReplacingCharactersInRange:NSMakeRange(0, readable.length) withString:readable];
}

+ (NSString*)OSStatusString:(int)status
{
    char fourcc[8] = {};
    NSString* message;

    // See if it appears to be a 4-char-code.
    *(UInt32*)(fourcc + 1) = CFSwapInt32HostToBig(status);
    if (isprint(fourcc[1]) && isprint(fourcc[2]) && isprint(fourcc[3]) && isprint(fourcc[4])) {
        fourcc[0] = fourcc[5] = '\'';
        fourcc[6] = '\0';
        message = [NSString stringWithCString:(const char*)fourcc encoding:NSStringEncodingConversionAllowLossy];
    } else {
        // Otherwise try to get a human readable string from the NSError constructor.
        NSError* error = [NSError errorWithDomain:NSOSStatusErrorDomain code:status userInfo:nil];
        message = error.localizedFailureReason;
    }

    return message;
}

- (id)initWithLogViewController:(LogViewController*)lc
{
    self = [super init];
    if (self) {
        log = lc;
    }
    return self;
}

- (BOOL)setupWithError:(NSError**)error
{
    __weak MIDIController* weakSelf = self;

    portLight = 0;
    portKeyboard = 0;
    portControlSurface = 0;
    outputPortControlSurface = 0;
    controlSurfaceDestination = 0;
    sentControlSurfaceHandshake = NO;

    OSStatus status = MIDIClientCreateWithBlock((CFStringRef) @"KompleteSynthesia", &client,
                                                ^(const MIDINotification* _Nonnull message) {
                                                  if (message->messageID == kMIDIMsgSetupChanged) {
                                                      if (![weakSelf rescanMIDI]) {
                                                          NSLog(@"failed to create midi client interface connections");
                                                      }
                                                  }
                                                });
    if (status != 0) {
        NSLog(@"MIDIClientCreate: %d", status);
        if (error != nil) {
            NSDictionary* userInfo = @{
                NSLocalizedDescriptionKey :
                    [NSString stringWithFormat:@"MIDI Error: %@", [MIDIController OSStatusString:status]],
                NSLocalizedRecoverySuggestionErrorKey : @"Try switching it off and on again."
            };
            *error = [NSError errorWithDomain:[[NSBundle bundleForClass:[self class]] bundleIdentifier]
                                         code:status
                                     userInfo:userInfo];
        }
        return NO;
    }
    MIDIReceiveBlock receiveBlockLightLoopback = ^void(const MIDIEventList* evtlist, void* srcConRef) {
      [weakSelf receivedMIDIEvents:evtlist interface:kMIDIConnectionInterfaceLightLoopback];
    };

    // MIDIInputPortCreateWithProtocol does not exist on macOS 10.15. We could replace this
    // logic with `MIDIInputPortCreateWithBlock` which works based on MIDIPackets and not
    // MIDIEvents - that in turn makes the parser a more complex and prone to failurea. But
    // it would give us 10.15 (catalina) compatiblity.
    status = MIDIInputPortCreateWithProtocol(client, (__bridge CFStringRef)kMIDIInputInterfaceLightLoopback,
                                             kMIDIProtocol_1_0, &portLight, receiveBlockLightLoopback);
    if (status != 0) {
        NSLog(@"MIDIInputPortCreate: %d", status);
        if (error != nil) {
            NSDictionary* userInfo = @{
                NSLocalizedDescriptionKey :
                    [NSString stringWithFormat:@"MIDI Error: %@", [MIDIController OSStatusString:status]],
                NSLocalizedRecoverySuggestionErrorKey : @"Try to restart this application."
            };
            *error = [NSError errorWithDomain:[[NSBundle bundleForClass:[self class]] bundleIdentifier]
                                         code:status
                                     userInfo:userInfo];
        }
        return NO;
    }

    // It was so nice, we do it twice...

    MIDIReceiveBlock receiveBlockKeyboard = ^void(const MIDIEventList* evtlist, void* srcConRef) {
      [weakSelf receivedMIDIEvents:evtlist interface:kMIDIConnectionInterfaceKeyboard];
    };

    status = MIDIInputPortCreateWithProtocol(client, (__bridge CFStringRef)kMIDIInputInterfaceKeyboard,
                                             kMIDIProtocol_1_0, &portKeyboard, receiveBlockKeyboard);
    if (status != 0) {
        NSLog(@"MIDIInputPortCreate: %d", status);
        if (error != nil) {
            NSDictionary* userInfo = @{
                NSLocalizedDescriptionKey :
                    [NSString stringWithFormat:@"MIDI Error: %@", [MIDIController OSStatusString:status]],
                NSLocalizedRecoverySuggestionErrorKey : @"Try to restart this application."
            };
            *error = [NSError errorWithDomain:[[NSBundle bundleForClass:[self class]] bundleIdentifier]
                                         code:status
                                     userInfo:userInfo];
        }
        return NO;
    }

    // MK3's "DAW" port carries buttons/jogwheel as Mackie-Control-style MIDI. Non-fatal
    // if this fails to set up, MK1/MK2 simply have no port by this name.
    MIDIReceiveBlock receiveBlockControlSurface = ^void(const MIDIEventList* evtlist, void* srcConRef) {
      // The device acknowledges our handshake by echoing CC_HELLO back with the agreed
      // protocol version. Reply with our SysEx DAW-info identification and tempo, or it
      // won't proceed past the handshake and stays silent for everything else.
      // TEMPORARY diagnostic, confirms whether anything is still arriving on this port
      // at all. Remove once button dispatch is confirmed working again.
      const MIDIEventPacket* p = &evtlist->packet[0];
      for (unsigned int i = 0; i < evtlist->numPackets; i++) {
          NSMutableString* words = [NSMutableString string];
          for (unsigned int w = 0; w < p->wordCount; w++) {
              UInt8 status = (p->words[w] & 0x00F00000) >> 20;
              UInt8 channel = (p->words[w] & 0x000F0000) >> 16;
              UInt8 data1 = (p->words[w] & 0x0000FF00) >> 8;
              [words appendFormat:@"%08x ", p->words[w]];
              if (status == kMIDICVStatusControlChange && channel == kMIDIControlSurfaceChannel &&
                  data1 == kMIDIControlSurfaceHandshakeCC) {
                  NSLog(@"control surface acknowledged handshake, sending DAW info and tempo");
                  [weakSelf sendControlSurfaceDAWInfo];
                  [weakSelf sendControlSurfaceTempo:120.0];
              }
          }
          NSLog(@"control surface: raw packet %u: %@", i, words);
          p = MIDIEventPacketNext(p);
      }
      [weakSelf receivedMIDIEvents:evtlist interface:kMIDIConnectionInterfaceControlSurface];
    };

    status = MIDIInputPortCreateWithProtocol(client, (__bridge CFStringRef)kMIDIInputInterfaceControlSurfaceMK3,
                                             kMIDIProtocol_1_0, &portControlSurface, receiveBlockControlSurface);
    if (status != 0) {
        NSLog(@"MIDIInputPortCreate (control surface): %d", status);
    }

    // Needed to send the NIHIA handshake below, the control surface stays silent
    // without it.
    status = MIDIOutputPortCreate(client, (CFStringRef) @"KompleteSynthesiaControlSurfaceOut",
                                  &outputPortControlSurface);
    if (status != 0) {
        NSLog(@"MIDIOutputPortCreate (control surface): %d", status);
    }

    if ([self rescanMIDI] == NO) {
        NSLog(@"MIDI interfaces ports not found");
        if (error != nil) {
            NSDictionary* userInfo = @{
                NSLocalizedDescriptionKey :
                    [NSString stringWithFormat:@"MIDI interface ports \'%@\' and \'%@' not found",
                                               kMIDIInputInterfaceLightLoopback, kMIDIInputInterfaceKeyboard],
                NSLocalizedRecoverySuggestionErrorKey : @"Make sure you setup the interface port as documented."
            };
            *error = [NSError errorWithDomain:[[NSBundle bundleForClass:[self class]] bundleIdentifier]
                                         code:-1
                                     userInfo:userInfo];
        }
        return NO;
    }
    return YES;
}

- (NSString*)status
{
    return connected ? [NSString stringWithFormat:@"Receiving from %@", kMIDIInputInterfaceLightLoopback]
                     : @"Interface not found";
}

- (void)dealloc
{
    if (portLight != 0) {
        MIDIPortDispose(portLight);
    }
    if (portKeyboard != 0) {
        MIDIPortDispose(portKeyboard);
    }
    if (portControlSurface != 0) {
        MIDIPortDispose(portControlSurface);
    }
    if (outputPortControlSurface != 0) {
        if (sentControlSurfaceHandshake) {
            [self sendControlSurfaceCC:kMIDIControlSurfaceGoodbyeCC value:0];
        }
        MIDIPortDispose(outputPortControlSurface);
    }
    if (client != 0) {
        MIDIClientDispose(client);
    }
}

- (BOOL)rescanMIDI
{
    NSLog(@"midi configuration changed");
    BOOL connectedToLightLoopback = NO;
    BOOL connectedToKeyboard = NO;

    // Try to locate the input endpoints we are configured for and connect.
    // FIXME: This seems not entirely correct - the MIDI input scanning and
    // FIXME: connection setup seems weirdly redundant the way this is now implemented.
    // FIXME: But hey, it works for me!
    MIDIEndpointRef source = 0;
    for (ItemCount i = 0; i < MIDIGetNumberOfSources(); ++i) {
        source = MIDIGetSource(i);
        if (source != 0) {
            MIDIEntityRef entity = 0;
            OSStatus status = MIDIEndpointGetEntity(source, &entity);
            if (status != 0) {
                NSLog(@"MIDIEndpointGetEntity: %d", status);
                continue;
            }

            CFPropertyListRef pl = NULL;
            status = MIDIObjectGetProperties(entity, &pl, true);
            if (status != 0) {
                NSLog(@"MIDIObjectGetProperties: %d", status);
                continue;
            }
            NSDictionary* dictionary = (__bridge NSDictionary*)pl;
            NSString* name = [dictionary valueForKey:@"name"];
            NSLog(@"input name:  %@", name);
            if ([name compare:kMIDIInputInterfaceLightLoopback] == NSOrderedSame) {
                NSLog(@"found light loopback interface");
                status = MIDIPortConnectSource(portLight, source, NULL);
                if (status != 0) {
                    NSLog(@"MIDIPortConnectSource: %d", status);
                    return NO;
                }
                connectedToLightLoopback = YES;
            }
            if ([name compare:kMIDIInputInterfaceKeyboard] == NSOrderedSame ||
                [name compare:kMIDIInputInterfaceKeyboardMK3] == NSOrderedSame) {
                NSLog(@"found keyboard interface");
                status = MIDIPortConnectSource(portKeyboard, source, NULL);
                if (status != 0) {
                    NSLog(@"MIDIPortConnectSource: %d", status);
                    return NO;
                }
                connectedToKeyboard = YES;
            }
            if (portControlSurface != 0 && [name compare:kMIDIInputInterfaceControlSurfaceMK3] == NSOrderedSame) {
                NSLog(@"found control surface interface");
                status = MIDIPortConnectSource(portControlSurface, source, NULL);
                if (status != 0) {
                    NSLog(@"MIDIPortConnectSource (control surface): %d", status);
                }
            }
        }
    }
    // Locate the "DAW" destination so we can send the NIHIA handshake. The control
    // surface won't emit anything on the input side until it receives this.
    if (outputPortControlSurface != 0 && !sentControlSurfaceHandshake) {
        MIDIEndpointRef destination = 0;
        for (ItemCount i = 0; i < MIDIGetNumberOfDestinations(); ++i) {
            destination = MIDIGetDestination(i);
            if (destination == 0) {
                continue;
            }
            MIDIEntityRef entity = 0;
            if (MIDIEndpointGetEntity(destination, &entity) != 0) {
                continue;
            }
            CFPropertyListRef pl = NULL;
            if (MIDIObjectGetProperties(entity, &pl, true) != 0) {
                continue;
            }
            NSDictionary* dictionary = (__bridge NSDictionary*)pl;
            NSString* name = [dictionary valueForKey:@"name"];
            if ([name compare:kMIDIInputInterfaceControlSurfaceMK3] == NSOrderedSame) {
                controlSurfaceDestination = destination;
                NSLog(@"found control surface output, sending NIHIA handshake");
                [self sendControlSurfaceCC:kMIDIControlSurfaceHandshakeCC value:kMIDIControlSurfaceProtocolVersion];
                sentControlSurfaceHandshake = YES;
                // The control surface going silent (and separately, note velocity
                // sticking at 127) until a physical replug is caused by entering the
                // MK3's HID legacy LED mode for lightguide writes (HIDController.m):
                // it kills this DAW-port NIHIA session below the MIDI layer, confirmed
                // via MIDI Monitor to be unrecoverable by resending this handshake. See
                // TODO.md for the real fix in progress.
                break;
            }
        }
    }

    if (connectedToLightLoopback) {
        connected = YES;
    }
    return connected;
}

// See https://github.com/git-moss/DrivenByMoss KontrolProtocolControlSurface.java:
// sends a MIDI CC on channel 16 to the control surface's "DAW" MIDI destination. CC 1
// (with the desired protocol version as value) wakes up the control surface; CC 2 tells
// it to stop.
- (void)sendControlSurfaceCC:(UInt8)cc value:(UInt8)value
{
    if (outputPortControlSurface == 0 || controlSurfaceDestination == 0) {
        return;
    }

    Byte buffer[128];
    MIDIPacketList* packetList = (MIDIPacketList*)buffer;
    MIDIPacket* packet = MIDIPacketListInit(packetList);
    const Byte message[3] = {(Byte)(0xB0 | kMIDIControlSurfaceChannel), cc, value};
    packet = MIDIPacketListAdd(packetList, sizeof(buffer), packet, 0, sizeof(message), message);
    if (packet == NULL) {
        NSLog(@"failed to build control surface CC packet");
        return;
    }

    OSStatus status = MIDISend(outputPortControlSurface, controlSurfaceDestination, packetList);
    if (status != 0) {
        NSLog(@"MIDISend (control surface CC %u): %d", cc, (int)status);
    }
}

// Completes the NIHIA handshake: identifies us as a DAW via SysEx once the device has
// acknowledged CC_HELLO. Without this the device never proceeds past the handshake.
- (void)sendControlSurfaceDAWInfo
{
    if (outputPortControlSurface == 0 || controlSurfaceDestination == 0) {
        return;
    }

    const char* name = "KompleteSynthesia";
    size_t nameLength = strlen(name);
    size_t headerLength = sizeof(kMIDIControlSurfaceSysExHeader);
    // header + identity byte + versionMajor + versionMinor + name + trailing F7.
    size_t payloadLength = headerLength + 3 + nameLength + 1;

    Byte payload[64];
    assert(payloadLength <= sizeof(payload));
    size_t offset = 0;
    memcpy(payload, kMIDIControlSurfaceSysExHeader, headerLength);
    offset += headerLength;
    payload[offset++] = kMIDIControlSurfaceSysExIdentity;
    payload[offset++] = 1; // DAW version major, arbitrary, the device doesn't gate on it.
    payload[offset++] = 0; // DAW version minor.
    memcpy(payload + offset, name, nameLength);
    offset += nameLength;
    payload[offset++] = 0xF7;

    Byte buffer[128];
    MIDIPacketList* packetList = (MIDIPacketList*)buffer;
    MIDIPacket* packet = MIDIPacketListInit(packetList);
    packet = MIDIPacketListAdd(packetList, sizeof(buffer), packet, 0, payloadLength, payload);
    if (packet == NULL) {
        NSLog(@"failed to build control surface DAW-info SysEx packet");
        return;
    }

    OSStatus status = MIDISend(outputPortControlSurface, controlSurfaceDestination, packetList);
    if (status != 0) {
        NSLog(@"MIDISend (control surface DAW info): %d", (int)status);
    }
}

// Sends the current tempo, encoded as ten-nanosecond units per minute across 5 base-128
// bytes, matching the reference implementation's sendTempo().
- (void)sendControlSurfaceTempo:(double)bpm
{
    if (outputPortControlSurface == 0 || controlSurfaceDestination == 0) {
        return;
    }

    long long nsPerMinute = (long long)(kMIDIControlSurfaceTenNsPerMinute / bpm);
    Byte data[8];
    data[0] = kMIDIControlSurfaceSysExSetTempo;
    data[1] = 0;
    data[2] = 0;
    for (int i = 0; i < 5; i++) {
        data[3 + i] = (Byte)((nsPerMinute >> (i * 7)) & 0x7F);
    }

    size_t headerLength = sizeof(kMIDIControlSurfaceSysExHeader);
    size_t payloadLength = headerLength + sizeof(data) + 1;
    Byte payload[32];
    assert(payloadLength <= sizeof(payload));
    size_t offset = 0;
    memcpy(payload, kMIDIControlSurfaceSysExHeader, headerLength);
    offset += headerLength;
    memcpy(payload + offset, data, sizeof(data));
    offset += sizeof(data);
    payload[offset++] = 0xF7;

    Byte buffer[64];
    MIDIPacketList* packetList = (MIDIPacketList*)buffer;
    MIDIPacket* packet = MIDIPacketListInit(packetList);
    packet = MIDIPacketListAdd(packetList, sizeof(buffer), packet, 0, payloadLength, payload);
    if (packet == NULL) {
        NSLog(@"failed to build control surface tempo SysEx packet");
        return;
    }

    OSStatus status = MIDISend(outputPortControlSurface, controlSurfaceDestination, packetList);
    if (status != 0) {
        NSLog(@"MIDISend (control surface tempo): %d", (int)status);
    }
}

- (void)receivedMIDIEvents:(const MIDIEventList*)eventList interface:(unsigned char)interface
{
    const unsigned int kUMPStatusMask = 0x00F00000;
    const unsigned int kUMPStatusShift = 20;

    const unsigned int kUMPChannelMask = 0x000F0000;
    const unsigned int kUMPChannelShift = 16;

    const unsigned int kUMPParam1Mask = 0x0000FF00;
    const unsigned int kUMPParam1Shift = 8;

    const unsigned int kUMPParam2Mask = 0x000000FF;
    const unsigned int kUMPParam2Shift = 0;

    const MIDIEventPacket* packet = &eventList->packet[0];

    for (unsigned int i = 0; i < eventList->numPackets; i++) {
        for (unsigned int w = 0; w < packet->wordCount; w++) {
            unsigned char cvStatus = (packet->words[w] & kUMPStatusMask) >> kUMPStatusShift;

            // Skip packets with a status that we are not interested in.
            if (cvStatus != kMIDICVStatusNoteOn && cvStatus != kMIDICVStatusNoteOff &&
                cvStatus != kMIDICVStatusControlChange) {
                continue;
            }

            // Fully parse note-on/off & control change packets and pass them on to the delegate.
            unsigned char channel = (packet->words[w] & kUMPChannelMask) >> kUMPChannelShift;
            unsigned char param1 = (packet->words[w] & kUMPParam1Mask) >> kUMPParam1Shift;
            unsigned char param2 = (packet->words[w] & kUMPParam2Mask) >> kUMPParam2Shift;

            [self.delegate receivedMIDIEvent:cvStatus channel:channel param1:param1 param2:param2 interface:interface];
        }
        packet = MIDIEventPacketNext(packet);
    }
}

@end
