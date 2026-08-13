/*
 * Deliberately constrained experiment: test whether an S-series MK3 accepts
 * the legacy MK2 0x84 RGB565 rectangle command on USB interface 3 endpoint 4.
 * Non-write modes never open a USB device. The write mode can send only one
 * fixed 64x64 checkerboard—no custom geometry, payload, loop, retry, or clear.
 */

#import <Foundation/Foundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/USB.h>
#import <IOUSBHost/IOUSBHost.h>

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    kNIVendorID = 0x17cc,
    kS49MK3ProductID = 0x2100,
    kS61MK3ProductID = 0x2110,
    kS88MK3ProductID = 0x2120,
    kMK3InterfaceNumber = 3,
    kMK3BulkOutEndpoint = 4,
    kProbeWidth = 64,
    kProbeHeight = 64,
    kProbePixelBytes = kProbeWidth * kProbeHeight * 2,
    kProbePacketBytes = 8 + 8 + 6 + 2 + kProbePixelBytes + 12,
};

static const char *const kArmToken = "I_AM_WATCHING";

static bool is_mk3_product(uint16_t product)
{
    return product == kS49MK3ProductID || product == kS61MK3ProductID || product == kS88MK3ProductID;
}

static void put_be16(uint8_t *destination, uint16_t value)
{
    destination[0] = (uint8_t)(value >> 8);
    destination[1] = (uint8_t)value;
}

static uint16_t get_be16(const uint8_t *source)
{
    return (uint16_t)(((uint16_t)source[0] << 8) | source[1]);
}

static size_t build_probe_packet(uint8_t *packet, size_t capacity)
{
    if (capacity < kProbePacketBytes) {
        return 0;
    }

    size_t offset = 0;
    const uint8_t header[] = {0x84, 0x00, 0x00, 0x60, 0x00, 0x00, 0x00, 0x00};
    memcpy(packet + offset, header, sizeof(header));
    offset += sizeof(header);

    /* x, y, width, height in network byte order. */
    put_be16(packet + offset, 0);
    put_be16(packet + offset + 2, 0);
    put_be16(packet + offset + 4, kProbeWidth);
    put_be16(packet + offset + 6, kProbeHeight);
    offset += 8;

    const uint8_t pixel_preamble[] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x00};
    memcpy(packet + offset, pixel_preamble, sizeof(pixel_preamble));
    offset += sizeof(pixel_preamble);

    /* Pixel byte length expressed as a count of 32-bit words. */
    put_be16(packet + offset, kProbePixelBytes / 4);
    offset += 2;

    /* High-contrast 8x8 RGB565 checkerboard, most-significant byte first. */
    for (unsigned int y = 0; y < kProbeHeight; ++y) {
        for (unsigned int x = 0; x < kProbeWidth; ++x) {
            uint16_t pixel = (((x / 8) + (y / 8)) & 1) ? 0x07e0 : 0xf81f;
            put_be16(packet + offset, pixel);
            offset += 2;
        }
    }

    const uint8_t present[] = {0x02, 0x00, 0x00, 0x00, 0x03, 0x00,
                               0x00, 0x00, 0x40, 0x00, 0x00, 0x00};
    memcpy(packet + offset, present, sizeof(present));
    offset += sizeof(present);
    return offset;
}

static int selftest(void)
{
    uint8_t packet[kProbePacketBytes];
    memset(packet, 0xa5, sizeof(packet));
    size_t length = build_probe_packet(packet, sizeof(packet));
    const uint8_t header[] = {0x84, 0x00, 0x00, 0x60, 0x00, 0x00, 0x00, 0x00};
    const uint8_t preamble[] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x00};
    const uint8_t present[] = {0x02, 0x00, 0x00, 0x00, 0x03, 0x00,
                               0x00, 0x00, 0x40, 0x00, 0x00, 0x00};
    const size_t pixels = 8 + 8 + 6 + 2;

#define CHECK(condition, message)                                                                        \
    do {                                                                                                 \
        if (!(condition)) {                                                                              \
            fprintf(stderr, "SELFTEST FAILED: %s\n", message);                                          \
            return 1;                                                                                    \
        }                                                                                                \
    } while (0)

    CHECK(build_probe_packet(packet, sizeof(packet) - 1) == 0, "undersized destination accepted");
    CHECK(length == kProbePacketBytes, "packet length");
    CHECK(memcmp(packet, header, sizeof(header)) == 0, "0x84 header");
    CHECK(get_be16(packet + 8) == 0 && get_be16(packet + 10) == 0, "rectangle origin");
    CHECK(get_be16(packet + 12) == kProbeWidth && get_be16(packet + 14) == kProbeHeight,
          "rectangle dimensions");
    CHECK(memcmp(packet + 16, preamble, sizeof(preamble)) == 0, "pixel preamble");
    CHECK(get_be16(packet + 22) == kProbePixelBytes / 4, "pixel word count");
    CHECK(get_be16(packet + pixels) == 0xf81f, "top-left magenta pixel");
    CHECK(get_be16(packet + pixels + 8 * 2) == 0x07e0, "top-row green tile");
    CHECK(get_be16(packet + pixels + 8 * kProbeWidth * 2) == 0x07e0, "second-row green tile");
    CHECK(get_be16(packet + pixels + (kProbeWidth * kProbeHeight - 1) * 2) == 0xf81f,
          "bottom-right magenta pixel");
    CHECK(memcmp(packet + length - sizeof(present), present, sizeof(present)) == 0, "present trailer");

#undef CHECK

    printf("SELFTEST PASSED: fixed %dx%d packet is %zu bytes; no USB device was opened.\n", kProbeWidth,
           kProbeHeight, length);
    return 0;
}

static bool registry_u16(io_service_t service, CFStringRef key, uint16_t *result)
{
    CFTypeRef value = IORegistryEntryCreateCFProperty(service, key, kCFAllocatorDefault, 0);
    int number = 0;
    bool found = value != NULL && CFGetTypeID(value) == CFNumberGetTypeID() &&
                 CFNumberGetValue((CFNumberRef)value, kCFNumberIntType, &number);
    if (value != NULL) {
        CFRelease(value);
    }
    if (found) {
        *result = (uint16_t)number;
    }
    return found;
}

static io_service_t find_mk3_interface(uint16_t *product_id)
{
    io_iterator_t iterator = IO_OBJECT_NULL;
    IOReturn result = IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOUSBHostInterface"),
                                                    &iterator);
    if (result != kIOReturnSuccess) {
        return IO_OBJECT_NULL;
    }

    io_service_t service;
    while ((service = IOIteratorNext(iterator)) != IO_OBJECT_NULL) {
        uint16_t vendor = 0;
        uint16_t product = 0;
        uint16_t interface_number = UINT16_MAX;
        if (registry_u16(service, CFSTR("idVendor"), &vendor) &&
            registry_u16(service, CFSTR("idProduct"), &product) &&
            registry_u16(service, CFSTR("bInterfaceNumber"), &interface_number) && vendor == kNIVendorID &&
            is_mk3_product(product) && interface_number == kMK3InterfaceNumber) {
            *product_id = product;
            IOObjectRelease(iterator);
            return service;
        }
        IOObjectRelease(service);
    }
    IOObjectRelease(iterator);
    return IO_OBJECT_NULL;
}

static int write_one_rectangle(void)
{
    uint16_t product_id = 0;
    io_service_t service = find_mk3_interface(&product_id);
    if (service == IO_OBJECT_NULL) {
        fprintf(stderr, "No supported S49/S61/S88 MK3 interface 3 found. Nothing was written.\n");
        return 1;
    }
    printf("Found Native Instruments MK3 product 0x%04x.\n", product_id);

    NSError *error = nil;
    IOUSBHostInterface *interface = [[IOUSBHostInterface alloc]
        initWithIOService:service
                  options:IOUSBHostObjectInitOptionsNone
                    queue:nil
                    error:&error
          interestHandler:nil];
    IOObjectRelease(service);
    if (interface == nil) {
        fprintf(stderr, "Could not claim USB interface %d: %s. Hardware Connection Service may still own it. "
                        "Nothing was written.\n",
                kMK3InterfaceNumber, error.localizedDescription.UTF8String);
        return 1;
    }

    IOUSBHostPipe *pipe = [interface copyPipeWithAddress:kMK3BulkOutEndpoint error:&error];
    if (pipe == nil) {
        fprintf(stderr, "Bulk OUT endpoint %d not found: %s. Nothing was written.\n", kMK3BulkOutEndpoint,
                error.localizedDescription.UTF8String);
        [interface destroy];
        return 1;
    }
    const IOUSBHostIOSourceDescriptors *descriptors = pipe.descriptors;
    const uint8_t transfer_type = descriptors->descriptor.bmAttributes & kUSB_EPDesc_bmAttributes_TranType_Mask;
    if (pipe.endpointAddress != kMK3BulkOutEndpoint || transfer_type != kUSBBulk) {
        fprintf(stderr, "Endpoint guard failed: address 0x%02lx, transfer type %u. Nothing was written.\n",
                (unsigned long)pipe.endpointAddress, transfer_type);
        pipe = nil;
        [interface destroy];
        return 1;
    }

    uint8_t packet[kProbePacketBytes];
    size_t length = build_probe_packet(packet, sizeof(packet));
    if (length != sizeof(packet)) {
        fprintf(stderr, "Internal packet assembly failure. Nothing was written.\n");
        pipe = nil;
        [interface destroy];
        return 1;
    }
    const uint16_t max_packet_size = USBToHostWord(descriptors->descriptor.wMaxPacketSize);
    printf("Sending exactly one %dx%d checkerboard (%zu bytes) to interface %d, endpoint %d "
           "(max packet %u).\n",
           kProbeWidth, kProbeHeight, length, kMK3InterfaceNumber, kMK3BulkOutEndpoint, max_packet_size);
    NSMutableData *data = [NSMutableData dataWithBytes:packet length:length];
    NSUInteger bytes_transferred = 0;
    BOOL success = [pipe sendIORequestWithData:data
                              bytesTransferred:&bytes_transferred
                             completionTimeout:1.0
                                         error:&error];
    pipe = nil;
    [interface destroy];

    if (!success || bytes_transferred != length) {
        fprintf(stderr, "Single USB write failed after %lu/%zu bytes: %s. Interface released; no retry "
                        "attempted.\n",
                (unsigned long)bytes_transferred, length, error.localizedDescription.UTF8String);
        return 1;
    }
    printf("Single USB write completed; interface released. No clear or second write was sent.\n");
    return 0;
}

static int inspect_device(void)
{
    uint16_t product_id = 0;
    io_service_t service = find_mk3_interface(&product_id);
    if (service == IO_OBJECT_NULL) {
        fprintf(stderr, "No supported S49/S61/S88 MK3 interface 3 found. No interface was opened.\n");
        return 1;
    }
    IOObjectRelease(service);
    printf("Found Native Instruments MK3 product 0x%04x. No interface was opened and nothing was written.\n",
           product_id);
    return 0;
}

static void print_plan(const char *program)
{
    printf("MK3 raw-display probe (safe mode)\n\n");
    printf("This invocation does not enumerate or open USB devices.\n");
    printf("Offline verification:  %s --selftest\n", program);
    printf("Read-only detection:    %s --inspect-device\n", program);
    printf("Armed hardware write:  %s --write-one-rectangle %s\n\n", program, kArmToken);
    printf("Armed mode is fixed to one 64x64 RGB565 checkerboard at screen 0, x=0, y=0, using legacy "
           "command 0x84 on MK3 interface 3 endpoint 4.\n");
}

int main(int argc, char **argv)
{
    @autoreleasepool {
    if (argc == 1 || (argc == 2 && strcmp(argv[1], "--plan") == 0)) {
        print_plan(argv[0]);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--selftest") == 0) {
        return selftest();
    }
    if (argc == 2 && strcmp(argv[1], "--inspect-device") == 0) {
        return inspect_device();
    }
    if (argc == 3 && strcmp(argv[1], "--write-one-rectangle") == 0 && strcmp(argv[2], kArmToken) == 0) {
        return write_one_rectangle();
    }
    fprintf(stderr, "Refusing invocation: exact arming arguments are required for hardware-writing mode.\n");
    print_plan(argv[0]);
        return 2;
    }
}
