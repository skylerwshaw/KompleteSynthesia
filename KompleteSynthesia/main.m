//
//  main.m
//  KompleteSynthesia
//
//  Created by Till Toenshoff on 28.12.22.
//

#import <Cocoa/Cocoa.h>

#import <string.h>

#import "ODRClient.h"

int main(int argc, const char* argv[])
{
    @autoreleasepool {
        for (int i = 1; i < argc; i++) {
            if (strcmp(argv[i], "--selftest") == 0) {
                return [ODRClient runEncodingSelfTest] ? 0 : 1;
            }
        }
    }
    return NSApplicationMain(argc, argv);
}
