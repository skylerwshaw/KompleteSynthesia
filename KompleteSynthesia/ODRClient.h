//
//  ODRClient.h
//  KompleteSynthesia
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@class LogViewController;

// Lights S-series MK3 keys through Native Instruments' NIHardwareConnectionService rather
// than by driving the device ourselves.
//
// The service owns the keyboard over USB interface 3 and exposes it as an msgpack-RPC
// service on a Unix socket; Komplete Kontrol is simply one of its clients, and so is this.
// Lighting this way leaves the control surface working, because it never enters the legacy
// HID `A0 00 00` LED mode that kills the DAW-port NIHIA session (see TODO.md).
//
// Protocol, method numbers and the two silent-failure traps are documented in
// ODR_PROTOCOL.md. scripts/odr_lightguide.py is the reference implementation, and its
// --selftest checks the same handshake encoding this class builds.
@interface ODRClient : NSObject

// YES when the service's socket exists, i.e. NIHardwareConnectionService is up. Cheap
// enough to call before deciding whether this path is worth attempting.
+ (BOOL)serviceAvailable;

- (instancetype)initWithLogViewController:(nullable LogViewController*)logViewController;

// Handshakes, attaches to the keyboard with the given USB serial and takes focus. The
// serial is the one the device reports over HID/IOKit, the service addresses devices by
// it, so no reply parsing is needed to discover it.
- (BOOL)connectToDeviceWithSerial:(NSString*)serial error:(NSError**)error;

// Pushes the full key map. `colors` holds one byte per physical key in the same encoding
// the legacy path uses (kKompleteKontrolColor* | intensity), and `firstNote` is the MIDI
// note of key 0, the service's LED array is indexed by MIDI note, not by key.
- (BOOL)setKeyColors:(const unsigned char*)colors count:(size_t)count firstNote:(int)firstNote;

- (void)disconnect;

@property (nonatomic, readonly, getter=isConnected) BOOL connected;

@end

NS_ASSUME_NONNULL_END
