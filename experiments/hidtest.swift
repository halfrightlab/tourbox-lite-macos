import Foundation
import IOKit.hid

// IOHIDUserDevice 系列未在 Swift 头文件中导出，手动绑定符号
typealias IOHIDUserDeviceRef = OpaquePointer

@_silgen_name("IOHIDUserDeviceCreate")
func IOHIDUserDeviceCreate(_ allocator: CFAllocator?, _ properties: CFDictionary) -> IOHIDUserDeviceRef?

@_silgen_name("IOHIDUserDeviceHandleReport")
func IOHIDUserDeviceHandleReport(_ device: IOHIDUserDeviceRef,
                                _ report: UnsafePointer<UInt8>,
                                _ reportLength: CFIndex) -> IOReturn

// 标准键盘报告描述符：8 字节报告 = 1 修饰键 + 1 保留 + 6 键码
let desc: [UInt8] = [
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x06,        // Usage (Keyboard)
    0xA1, 0x01,        // Collection (Application)
    0x05, 0x07,        //   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,        //   Usage Min (LeftControl)
    0x29, 0xE7,        //   Usage Max (Right GUI)
    0x15, 0x00,        //   Logical Min (0)
    0x25, 0x01,        //   Logical Max (1)
    0x75, 0x01,        //   Report Size (1)
    0x95, 0x08,        //   Report Count (8)
    0x81, 0x02,        //   Input (Data,Var,Abs)
    0x95, 0x01,        //   Report Count (1)
    0x75, 0x08,        //   Report Size (8)
    0x81, 0x01,        //   Input (Const)
    0x95, 0x06,        //   Report Count (6)
    0x75, 0x08,        //   Report Size (8)
    0x15, 0x00,        //   Logical Min (0)
    0x25, 0x65,        //   Logical Max (101)
    0x05, 0x07,        //   Usage Page (Keyboard)
    0x19, 0x00,        //   Usage Min (0)
    0x29, 0x65,        //   Usage Max (101)
    0x81, 0x00,        //   Input (Data,Array)
    0xC0               // End Collection
]

let props: [String: Any] = [
    kIOHIDReportDescriptorKey as String: Data(desc),
    kIOHIDVendorIDKey as String: 0xCAFE,
    kIOHIDProductIDKey as String: 0x4002,
    kIOHIDProductKey as String: "TourBox Virtual Keyboard",
    kIOHIDTransportKey as String: "USB",
    kIOHIDVersionNumberKey as String: 1,
    kIOHIDCountryCodeKey as String: 0,
    kIOHIDPrimaryUsagePageKey as String: 0x01,
    kIOHIDPrimaryUsageKey as String: 0x06,
    kIOHIDSerialNumberKey as String: "TBVIRT0001",
]

guard let dev = IOHIDUserDeviceCreate(kCFAllocatorDefault, props as CFDictionary) else {
    print("FAIL: IOHIDUserDeviceCreate 返回 nil —— 需要 root 或「输入监控」权限")
    exit(1)
}
print("OK: 虚拟 HID 键盘创建成功")
print("请在 5 秒内点进任意输入框（备忘录/浏览器输入框），然后看语音输入是否启动…")
sleep(5)

func send(_ bytes: [UInt8]) {
    let ret = bytes.withUnsafeBufferPointer { buf in
        IOHIDUserDeviceHandleReport(dev, buf.baseAddress!, buf.count)
    }
    if ret != kIOReturnSuccess {
        print("  报告发送失败: \(ret)")
    }
}

// Left Shift(0x02 修饰位) + Space(HID usage 0x2C) 按住 2 秒
print("按住 Shift+Space ...")
send([0x02, 0x00, 0x2C, 0, 0, 0, 0, 0])
sleep(2)
print("释放")
send([0x00, 0x00, 0x00, 0, 0, 0, 0, 0])
print("完成")
