//
//  ScreenController.h
//  KompleteSynthesia
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@class ODRClient;
@class LogViewController;

// Drives the MK3 "Screen" (see CONTEXT.md): renders the now-playing notes as a 1280x212
// image and pushes it as the parameter-page background over the app's shared, focused
// ODRClient. This shares the one lighting connection deliberately, a second ODR client
// would fight over the single focus token (see docs/adr/0001).
//
// Off by default and only meaningful when the connected ODRClient reports screenSupported.
// Synthesia exposes no song title, progress, or tempo to us, only live notes, so the band
// shows note names colored by hand, with a branded idle state when nothing is playing.
@interface ScreenController : NSObject

- (instancetype)initWithODRClient:(ODRClient*)odrClient
                 logViewController:(nullable LogViewController*)logViewController;

// When NO (default), nothing is pushed and the device keeps its normal screen.
@property (nonatomic, getter=isEnabled) BOOL enabled;

// Currently-sounding notes drive the band. Callers feed note on/off with the hand already
// decoded (0 = left, 1 = right). Pushes are coalesced so rapid changes do not strobe the
// device, which blacks out for ~0.5s on every background change.
- (void)noteOn:(unsigned char)note hand:(int)hand;
- (void)noteOff:(unsigned char)note;
- (void)allNotesOff;

// Renders the current band and writes the PNG to `path`. No device needed; used to eyeball
// the layout on the Mac. Returns NO on write failure.
- (BOOL)dumpBandToPNG:(NSString*)path;

@end

NS_ASSUME_NONNULL_END
