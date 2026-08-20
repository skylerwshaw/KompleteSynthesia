//
//  ScreenController.m
//  KompleteSynthesia
//

#import "ScreenController.h"

#import <AppKit/AppKit.h>

#import "LogViewController.h"
#import "MIDIController.h"
#import "ODRClient.h"

// The MK3 parameter-page background band the device renders host images into.
static const CGFloat kScreenWidth = 1280.0;
static const CGFloat kScreenHeight = 212.0;

// The device blacks out on every background change, so pushes are coalesced: a burst of
// note changes collapses into one push no more often than this. Not an fps loop.
static const NSTimeInterval kScreenCoalesceFloor = 0.3;

@implementation ScreenController {
    ODRClient* odr;
    LogViewController* log;

    // note number -> hand (0 left, 1 right) for currently-sounding notes. Guarded by
    // @synchronized(self) because note events arrive off the main thread.
    NSMutableDictionary<NSNumber*, NSNumber*>* heldNotes;

    BOOL pushPending;
}

- (instancetype)initWithODRClient:(ODRClient*)odrClient logViewController:(LogViewController*)logViewController
{
    self = [super init];
    if (self) {
        odr = odrClient;
        log = logViewController;
        heldNotes = [NSMutableDictionary dictionary];
    }
    return self;
}

- (void)setEnabled:(BOOL)enabled
{
    if (_enabled == enabled) {
        return;
    }
    _enabled = enabled;
    // Show the current state (or the idle band) as soon as it is switched on.
    if (enabled) {
        [self setNeedsPush];
    }
}

#pragma mark - Note feed

- (void)noteOn:(unsigned char)note hand:(int)hand
{
    @synchronized(self) {
        heldNotes[@(note)] = @(hand);
    }
    [self setNeedsPush];
}

- (void)noteOff:(unsigned char)note
{
    @synchronized(self) {
        [heldNotes removeObjectForKey:@(note)];
    }
    [self setNeedsPush];
}

- (void)allNotesOff
{
    @synchronized(self) {
        [heldNotes removeAllObjects];
    }
    [self setNeedsPush];
}

#pragma mark - Coalesced push

// Coalescing state (pushPending) lives on the main queue only, so note events from any
// thread hop here rather than racing on it.
- (void)setNeedsPush
{
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.enabled == NO || self->pushPending) {
            return;
        }
        self->pushPending = YES;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(kScreenCoalesceFloor * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
                         self->pushPending = NO;
                         [self renderAndPush];
                       });
    });
}

- (void)renderAndPush
{
    if (self.enabled == NO || odr.screenSupported == NO) {
        return;
    }

    NSData* png = [self renderPNG];
    NSData* handle = [odr uploadImageAsset:png];
    if (handle == nil) {
        [log logLine:@"MK3 screen: asset upload failed"];
        return;
    }
    [odr showParameterPageBackgroundWithHandle:handle];
}

#pragma mark - Rendering

- (NSData*)renderPNG
{
    NSMutableArray<NSString*>* names = [NSMutableArray array];
    NSMutableArray<NSNumber*>* hands = [NSMutableArray array];
    @synchronized(self) {
        NSArray<NSNumber*>* sorted = [heldNotes.allKeys sortedArrayUsingSelector:@selector(compare:)];
        for (NSNumber* note in sorted) {
            NSString* name = [[MIDIController readableNote:(unsigned char)note.intValue]
                stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceCharacterSet]];
            [names addObject:name];
            [hands addObject:heldNotes[note]];
        }
    }
    return [self renderBandWithNames:names hands:hands];
}

- (NSData*)renderBandWithNames:(NSArray<NSString*>*)names hands:(NSArray<NSNumber*>*)hands
{
    NSBitmapImageRep* rep = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL
                                                                    pixelsWide:kScreenWidth
                                                                    pixelsHigh:kScreenHeight
                                                                 bitsPerSample:8
                                                               samplesPerPixel:4
                                                                      hasAlpha:YES
                                                                      isPlanar:NO
                                                                colorSpaceName:NSDeviceRGBColorSpace
                                                                   bytesPerRow:0
                                                                  bitsPerPixel:0];
    NSGraphicsContext* ctx = [NSGraphicsContext graphicsContextWithBitmapImageRep:rep];
    [NSGraphicsContext saveGraphicsState];
    [NSGraphicsContext setCurrentContext:ctx];

    [[NSColor blackColor] setFill];
    NSRectFill(NSMakeRect(0, 0, kScreenWidth, kScreenHeight));

    if (names.count == 0) {
        NSDictionary* attributes = @{
            NSFontAttributeName : [NSFont systemFontOfSize:44.0],
            NSForegroundColorAttributeName : [NSColor colorWithWhite:0.5 alpha:1.0]
        };
        NSString* idle = @"KompleteSynthesia";
        NSSize size = [idle sizeWithAttributes:attributes];
        [idle drawAtPoint:NSMakePoint((kScreenWidth - size.width) / 2.0, (kScreenHeight - size.height) / 2.0)
           withAttributes:attributes];
    } else {
        // Left hand blue, right hand green, matching Synthesia's finger-based lighting.
        NSColor* left = [NSColor colorWithRed:0.30 green:0.55 blue:1.00 alpha:1.0];
        NSColor* right = [NSColor colorWithRed:0.30 green:0.90 blue:0.45 alpha:1.0];
        NSFont* font = [NSFont monospacedSystemFontOfSize:96.0 weight:NSFontWeightSemibold];

        NSMutableAttributedString* line = [[NSMutableAttributedString alloc] init];
        for (NSUInteger i = 0; i < names.count; i++) {
            NSColor* color = [hands[i] intValue] == 1 ? right : left;
            NSString* segment = i == 0 ? names[i] : [@"  " stringByAppendingString:names[i]];
            [line appendAttributedString:[[NSAttributedString alloc]
                                             initWithString:segment
                                                 attributes:@{
                                                     NSFontAttributeName : font,
                                                     NSForegroundColorAttributeName : color
                                                 }]];
        }
        NSSize size = [line size];
        [line drawAtPoint:NSMakePoint((kScreenWidth - size.width) / 2.0, (kScreenHeight - size.height) / 2.0)];
    }

    [NSGraphicsContext restoreGraphicsState];
    return [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
}

- (BOOL)dumpBandToPNG:(NSString*)path
{
    return [[self renderPNG] writeToFile:path atomically:YES];
}

@end
