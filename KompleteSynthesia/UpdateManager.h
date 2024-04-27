//
//  UpdateManager.h
//  KompleteSynthesia
//
//  Created by Till Toenshoff on 10.12.23.
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

extern NSString* kAppDefaultCheckForUpdate;

@interface UpdateManager : NSObject

+ (void)updateCheckWithCompletion:(void (^)(NSString* status))completion;
+ (BOOL)checkForUpdates;

@end

NS_ASSUME_NONNULL_END
