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

// Offline check of the msgpack `bin` encoding and SHA-256 asset handle the screen path
// builds. No socket, no hardware; run via `KompleteSynthesia --selftest`.
+ (BOOL)runEncodingSelfTest;

- (instancetype)initWithLogViewController:(nullable LogViewController*)logViewController;

// Handshakes, attaches to the keyboard with the given USB serial and takes focus. The
// serial is the one the device reports over HID/IOKit, the service addresses devices by
// it, so no reply parsing is needed to discover it.
- (BOOL)connectToDeviceWithSerial:(NSString*)serial error:(NSError**)error;

// Pushes the full key map. `colors` holds one byte per physical key in the same encoding
// the legacy path uses (kKompleteKontrolColor* | intensity), and `firstNote` is the MIDI
// note of key 0, the service's LED array is indexed by MIDI note, not by key.
- (BOOL)setKeyColors:(const unsigned char*)colors count:(size_t)count firstNote:(int)firstNote;

#pragma mark - Screen

// The MK3 "Screen" is the 1280x212 image band of the parameter page (see CONTEXT.md), set
// over this same focused session. Only agents new enough to expose `add_asset` in their
// symbol registry support it; NO on older agents, where lighting still works.
@property (nonatomic, readonly) BOOL screenSupported;

// Stores encoded image bytes (PNG or WebP; the device decodes) as a content-addressed
// asset via `add_asset`, and returns the 32-byte SHA-256 handle the device addresses it by
// (nil if unsupported, disconnected, or empty input). A page background then references
// that handle. The handle is always exactly 32 bytes: a wrong-length handle crashes the
// shared service and takes the light guide down with it, so this never emits one.
- (nullable NSData*)uploadImageAsset:(NSData*)imageBytes;

// Shows the given asset (returned by uploadImageAsset:) as the full-width parameter-page
// background, by replaying a captured page frame with our identity and this handle
// substituted in (see docs/adr/0001). NO if unsupported or the handle is not 32 bytes.
- (BOOL)showParameterPageBackgroundWithHandle:(NSData*)handle;

- (void)disconnect;

@property (nonatomic, readonly, getter=isConnected) BOOL connected;

@end

NS_ASSUME_NONNULL_END
