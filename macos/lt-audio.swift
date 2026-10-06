// lt-audio: the Core Audio side of 同声传译 on macOS, run by audio_mac.py and mt.py.
//
//   lt-audio devices              JSON on stdout: microphones, defaults, permission state
//   lt-audio tap [--no-request]   capture system audio through a process tap (macOS 14.2+)
//   lt-audio mic [UID|default]    capture a microphone ("default" follows the system default)
//   lt-audio watchdog PPID PID    SIGKILL PID as soon as PPID exits (no orphaned llama-server)
//
// Capture writes packets to stdout: "LTAU", sample rate (u32 LE), frame count (u32 LE),
// then that many mono float32 LE samples. Status goes to stderr, one JSON object per line:
//   {"event":"started","rate":48000,"device":"..."}   (again after every restart)
//   {"event":"permission","kind":"tap"|"mic","status":"requesting"}
//   {"event":"silent","others_playing":true}          tap gives only zeros while apps play
//   {"event":"error","code":"...","message":"..."}
// Exit codes: 0 parent gone / stdout closed, 1 failure, 2 unsupported, 3 permission denied.
//
// The capture device is rebuilt whenever the default device changes, the device dies or
// changes sample rate, or its IO stops calling back; like the WASAPI source on Windows.

import AVFoundation
import AudioToolbox
import CoreAudio
import Darwin
import Foundation

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func emit(_ event: String, _ fields: [String: Any] = [:]) {
    var obj = fields
    obj["event"] = event
    guard var data = try? JSONSerialization.data(withJSONObject: obj) else { return }
    data.append(0x0A)
    data.withUnsafeBytes { raw in
        _ = Darwin.write(2, raw.baseAddress, raw.count)
    }
}

struct Failure: Error {
    let code: String
    let message: String
    var exitCode: Int32 = 1
}

func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr {
        throw Failure(code: "coreaudio", message: "\(what) failed (\(status))")
    }
}

// MARK: - property helpers

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func readValue<T>(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T,
                  scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> T? {
    var addr = address(selector, scope)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &value) { ptr -> OSStatus in
        AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, ptr)
    }
    return status == noErr ? value : nil
}

func readString(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &value) { ptr -> OSStatus in
        AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, ptr)
    }
    guard status == noErr, let v = value else { return nil }
    return v.takeRetainedValue() as String
}

func readArray<T>(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector, of _: T.Type) -> [T] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    let count = Int(size) / MemoryLayout<T>.stride
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<T>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, raw) == noErr else { return [] }
    let typed = raw.bindMemory(to: T.self, capacity: count)
    return Array(UnsafeBufferPointer(start: typed, count: count))
}

func channelCount(_ dev: AudioObjectID, _ scope: AudioObjectPropertyScope) -> Int {
    var addr = address(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size),
                                               alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioObjectID {
    readValue(systemObject, selector, AudioObjectID(kAudioObjectUnknown)) ?? AudioObjectID(kAudioObjectUnknown)
}

func allDevices() -> [AudioObjectID] {
    readArray(systemObject, kAudioHardwarePropertyDevices, of: AudioObjectID.self)
}

func deviceName(_ dev: AudioObjectID) -> String {
    readString(dev, kAudioObjectPropertyName) ?? "?"
}

func processObject(_ pid: pid_t) -> AudioObjectID? {
    var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var qualifier = pid
    var obj = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(systemObject, &addr, UInt32(MemoryLayout<pid_t>.size), &qualifier,
                                            &size, &obj)
    return status == noErr && obj != AudioObjectID(kAudioObjectUnknown) ? obj : nil
}

/// Whether some other process is playing sound right now (used to tell a denied tap,
/// which delivers zeros without any error, from a Mac that is simply silent).
func othersPlaying() -> Bool {
    guard #available(macOS 14.2, *) else { return false }
    let me = getpid(), parent = getppid()
    for obj in readArray(systemObject, kAudioHardwarePropertyProcessObjectList, of: AudioObjectID.self) {
        let pid = readValue(obj, kAudioProcessPropertyPID, pid_t(0)) ?? 0
        if pid == me || pid == parent { continue }
        // recorders (another tap, a call) also keep an output device running: not playback
        if (readValue(obj, kAudioProcessPropertyIsRunningInput, UInt32(0)) ?? 0) != 0 { continue }
        if (readValue(obj, kAudioProcessPropertyIsRunningOutput, UInt32(0)) ?? 0) != 0 { return true }
    }
    return false
}

// MARK: - permissions

/// TCC has no public API for the system-audio-recording permission; this is the private
/// one every tap-based recorder uses (AudioCap and friends). 0 = granted, 1 = denied.
enum TCC {
    typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int
    typealias Request = @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void
    static let service = "kTCCServiceAudioCapture" as CFString
    static let handle = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)

    static func preflight() -> Int? {
        guard let h = handle, let sym = dlsym(h, "TCCAccessPreflight") else { return nil }
        return unsafeBitCast(sym, to: Preflight.self)(service, nil)
    }

    /// true/false once the user answered; nil when there is no SPI or nobody answered in time.
    static func request(timeout: Double) -> Bool? {
        guard let h = handle, let sym = dlsym(h, "TCCAccessRequest") else { return nil }
        let done = DispatchSemaphore(value: 0)
        let granted = UnsafeMutablePointer<Bool>.allocate(capacity: 1)
        granted.pointee = false
        unsafeBitCast(sym, to: Request.self)(service, nil) { ok in
            granted.pointee = ok
            done.signal()
        }
        if done.wait(timeout: .now() + timeout) == .timedOut { return nil }
        return granted.pointee
    }

    static var statusName: String {
        switch preflight() {
        case 0?: return "authorized"
        case 1?: return "denied"
        case nil: return "unavailable"
        default: return "undetermined"
        }
    }
}

func micStatusName() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "undetermined"
    @unknown default: return "unknown"
    }
}

var tapSupported: Bool {
    if #available(macOS 14.2, *) { return true }
    return false
}

// MARK: - output

/// Small heap cells shared between the IO thread and the control queue.
final class Shared {
    let lastCallback = UnsafeMutablePointer<Double>.allocate(capacity: 1)
    let lastSignal = UnsafeMutablePointer<Double>.allocate(capacity: 1)
    init() {
        lastCallback.pointee = 0
        lastSignal.pointee = 0
    }
}

func now() -> Double { Date().timeIntervalSince1970 }

func writeAll(_ data: Data) {
    data.withUnsafeBytes { raw in
        var p = raw.baseAddress!
        var left = raw.count
        while left > 0 {
            let n = Darwin.write(1, p, left)
            if n < 0 {
                if errno == EINTR { continue }
                exit(0)  // the app closed the pipe: nobody is listening any more
            }
            p += n
            left -= n
        }
    }
}

// MARK: - capture

final class Capture {
    enum Mode { case tap, mic(String) }

    let mode: Mode
    let noRequest: Bool
    let control = DispatchQueue(label: "lt-audio.control")
    let writer = DispatchQueue(label: "lt-audio.writer")
    let shared = Shared()
    var device = AudioObjectID(kAudioObjectUnknown)
    var tap = AudioObjectID(kAudioObjectUnknown)
    var aggregate = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?
    var deviceListeners: [(AudioObjectID, AudioObjectPropertyAddress, AudioObjectPropertyListenerBlock)] = []
    var running = false
    var startedAt = 0.0
    var warnedSilent = false
    var timer: DispatchSourceTimer?

    init(mode: Mode, noRequest: Bool) {
        self.mode = mode
        self.noRequest = noRequest
    }

    var isTap: Bool {
        if case .tap = mode { return true }
        return false
    }

    func run() {
        // Follow the system default: the output device feeds the tap's clock, and "default"
        // microphones should move to whatever the user picked in 系统设置.
        let followDefault: Bool
        switch mode {
        case .tap: followDefault = true
        case .mic(let uid): followDefault = uid == "default"
        }
        if followDefault {
            var addr = address(isTap ? kAudioHardwarePropertyDefaultOutputDevice : kAudioHardwarePropertyDefaultInputDevice)
            AudioObjectAddPropertyListenerBlock(systemObject, &addr, control) { [weak self] _, _ in
                self?.restart("default device changed")
            }
        }
        let t = DispatchSource.makeTimerSource(queue: control)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
        control.async { self.start() }
    }

    func restart(_ reason: String) {
        emit("restart", ["reason": reason])
        stop()
        start()
    }

    func start() {
        do {
            switch mode {
            case .tap:
                if #available(macOS 14.2, *) {
                    try startTap()
                } else {
                    throw Failure(code: "unsupported", message: "macOS 14.2 以下不支持采集系统声音", exitCode: 2)
                }
            case .mic(let uid):
                try startMic(uid)
            }
        } catch let f as Failure {
            stop()
            emit("error", ["code": f.code, "message": f.message])
            if f.exitCode != 1 { exit(f.exitCode) }
            control.asyncAfter(deadline: .now() + 2) { [weak self] in
                guard let self = self, !self.running else { return }
                self.start()
            }
        } catch {
            stop()
            emit("error", ["code": "failed", "message": "\(error)"])
            control.asyncAfter(deadline: .now() + 2) { [weak self] in
                guard let self = self, !self.running else { return }
                self.start()
            }
        }
    }

    func stop() {
        running = false
        for (obj, addr, block) in deviceListeners {
            var a = addr
            AudioObjectRemovePropertyListenerBlock(obj, &a, control, block)
        }
        deviceListeners.removeAll()
        if device != AudioObjectID(kAudioObjectUnknown), let pid = procID {
            AudioDeviceStop(device, pid)
            AudioDeviceDestroyIOProcID(device, pid)
        }
        procID = nil
        device = AudioObjectID(kAudioObjectUnknown)
        if aggregate != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyAggregateDevice(aggregate)
            aggregate = AudioObjectID(kAudioObjectUnknown)
        }
        if tap != AudioObjectID(kAudioObjectUnknown) {
            if #available(macOS 14.2, *) { AudioHardwareDestroyProcessTap(tap) }
            tap = AudioObjectID(kAudioObjectUnknown)
        }
    }

    func tick() {
        guard running else { return }
        let t = now()
        let last = shared.lastCallback.pointee
        // a freshly started device may take a few seconds before its first callback
        if (last < startedAt ? t - startedAt > 8 : t - last > 3) {
            restart("no audio callbacks")
            return
        }
        guard isTap else { return }
        let quiet = t - max(shared.lastSignal.pointee, startedAt)
        if warnedSilent {
            if quiet < 1 {  // sound again: the banner can go
                warnedSilent = false
                emit("signal")
            }
        } else if quiet > 8, TCC.preflight() != 0, othersPlaying() {
            // Without the permission a tap delivers zeros and no error; other apps playing
            // while nothing arrives is the only symptom (when TCC cannot be asked directly).
            warnedSilent = true
            emit("silent", ["others_playing": true, "permission": TCC.statusName])
        }
    }

    @available(macOS 14.2, *)
    func startTap() throws {
        switch TCC.preflight() {
        case 0?:
            break
        case 1?:
            throw Failure(code: "tap-denied", message: "没有录制系统声音的权限", exitCode: 3)
        default:
            if !noRequest {
                emit("permission", ["kind": "tap", "status": "requesting"])
                if TCC.request(timeout: 300) == false {
                    throw Failure(code: "tap-denied", message: "没有录制系统声音的权限", exitCode: 3)
                }
            }
        }
        let out = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
        guard out != AudioObjectID(kAudioObjectUnknown), let outUID = readString(out, kAudioDevicePropertyDeviceUID) else {
            throw Failure(code: "no-output", message: "找不到声音输出设备")
        }
        var excluded: [AudioObjectID] = []
        for pid in [getpid(), getppid()] {
            if let obj = processObject(pid) { excluded.append(obj) }
        }
        let desc = CATapDescription(monoGlobalTapButExcludeProcesses: excluded)
        desc.uuid = UUID()
        desc.name = "同声传译"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        var tapID = AudioObjectID(kAudioObjectUnknown)
        try check(AudioHardwareCreateProcessTap(desc, &tapID), "AudioHardwareCreateProcessTap")
        tap = tapID
        let format = readValue(tapID, kAudioTapPropertyFormat, AudioStreamBasicDescription())
        let tapChannels = max(1, Int(format?.mChannelsPerFrame ?? 1))

        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: "同声传译 系统声音",
            kAudioAggregateDeviceUIDKey: "com.haoawake.live-interpreter.tap." + UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                              kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        var agg = AudioObjectID(kAudioObjectUnknown)
        try check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &agg),
                  "AudioHardwareCreateAggregateDevice")
        aggregate = agg
        // The output device's own inputs (a USB headset's mic) come first in the
        // aggregate; the tap's channels are the last ones.
        try startIO(agg, name: deviceName(out), lastChannels: tapChannels, fallbackRate: format?.mSampleRate)
        watch(out, kAudioDevicePropertyDeviceIsAlive)
        watch(out, kAudioDevicePropertyNominalSampleRate)
    }

    func startMic(_ uid: String) throws {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            break
        case .notDetermined:
            emit("permission", ["kind": "mic", "status": "requesting"])
            let done = DispatchSemaphore(value: 0)
            let granted = UnsafeMutablePointer<Bool>.allocate(capacity: 1)
            granted.pointee = false
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                granted.pointee = ok
                done.signal()
            }
            if done.wait(timeout: .now() + 300) == .timedOut || !granted.pointee {
                throw Failure(code: "mic-denied", message: "没有使用麦克风的权限", exitCode: 3)
            }
        default:
            throw Failure(code: "mic-denied", message: "没有使用麦克风的权限", exitCode: 3)
        }
        var dev = AudioObjectID(kAudioObjectUnknown)
        if uid == "default" {
            dev = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
        } else {
            dev = allDevices().first { readString($0, kAudioDevicePropertyDeviceUID) == uid }
                ?? AudioObjectID(kAudioObjectUnknown)
        }
        guard dev != AudioObjectID(kAudioObjectUnknown), channelCount(dev, kAudioObjectPropertyScopeInput) > 0 else {
            throw Failure(code: "no-device", message: uid == "default" ? "找不到麦克风" : "找不到这个麦克风（已拔出？）")
        }
        try startIO(dev, name: deviceName(dev), lastChannels: nil, fallbackRate: nil)
        watch(dev, kAudioDevicePropertyDeviceIsAlive)
        watch(dev, kAudioDevicePropertyNominalSampleRate)
    }

    func watch(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) {
        var addr = address(selector)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            self?.restart(selector == kAudioDevicePropertyDeviceIsAlive ? "device gone" : "sample rate changed")
        }
        if AudioObjectAddPropertyListenerBlock(obj, &addr, control, block) == noErr {
            deviceListeners.append((obj, addr, block))
        }
    }

    func startIO(_ dev: AudioObjectID, name: String, lastChannels: Int?, fallbackRate: Float64?) throws {
        let nominal = readValue(dev, kAudioDevicePropertyNominalSampleRate, Float64(0)) ?? 0
        let rate = UInt32((nominal > 0 ? nominal : (fallbackRate ?? 48000)).rounded())
        let shared = self.shared
        let writer = self.writer
        var pid: AudioDeviceIOProcID?
        try check(AudioDeviceCreateIOProcIDWithBlock(&pid, dev, nil) { _, input, _, _, _ in
            Capture.process(input, rate: rate, lastChannels: lastChannels, shared: shared, writer: writer)
        }, "AudioDeviceCreateIOProcIDWithBlock")
        procID = pid
        device = dev
        try check(AudioDeviceStart(dev, pid), "AudioDeviceStart")
        running = true
        startedAt = now()
        warnedSilent = false
        emit("started", ["rate": Int(rate), "device": name, "source": isTap ? "tap" : "mic"])
    }

    /// IO thread: mix the wanted channels down to mono and hand the packet to the writer.
    static func process(_ input: UnsafePointer<AudioBufferList>, rate: UInt32, lastChannels: Int?,
                        shared: Shared, writer: DispatchQueue) {
        let t = now()
        shared.lastCallback.pointee = t
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        var total = 0
        for b in list { total += Int(b.mNumberChannels) }
        let want = min(lastChannels ?? total, total)
        guard want > 0 else { return }
        var skip = total - want
        var frames = 0
        var parts: [(UnsafePointer<Float>, Int, Int, Int)] = []  // data, stride, first channel, count
        for b in list {
            let ch = Int(b.mNumberChannels)
            guard ch > 0, let data = b.mData else { continue }
            let n = Int(b.mDataByteSize) / (4 * ch)
            let first = min(skip, ch)
            skip -= first
            if first < ch {
                parts.append((UnsafePointer(data.assumingMemoryBound(to: Float.self)), ch, first, ch - first))
                frames = frames == 0 ? n : min(frames, n)
            }
        }
        guard frames > 0 else { return }
        var packet = Data(count: 12 + frames * 4)
        var loud = false
        packet.withUnsafeMutableBytes { raw in
            let base = raw.baseAddress!
            let magic: [UInt8] = [0x4C, 0x54, 0x41, 0x55]  // "LTAU"
            base.copyMemory(from: magic, byteCount: 4)
            base.storeBytes(of: rate.littleEndian, toByteOffset: 4, as: UInt32.self)
            base.storeBytes(of: UInt32(frames).littleEndian, toByteOffset: 8, as: UInt32.self)
            let out = (base + 12).bindMemory(to: Float.self, capacity: frames)
            let scale = 1 / Float(want)
            for i in 0..<frames {
                var s: Float = 0
                for (data, stride, first, count) in parts {
                    let row = data + i * stride + first
                    for c in 0..<count { s += row[c] }
                }
                s *= scale
                if s != 0 { loud = true }
                out[i] = s
            }
        }
        if loud { shared.lastSignal.pointee = t }
        writer.async { writeAll(packet) }
    }
}

// MARK: - parent / watchdog

/// Exit (or run `action`) as soon as `pid` exits, even if nothing is being written.
func onExit(of pid: pid_t, _ action: @escaping () -> Void) -> Bool {
    let kq = kqueue()
    guard kq >= 0 else { return false }
    var ev = kevent(ident: UInt(pid), filter: Int16(EVFILT_PROC), flags: UInt16(EV_ADD | EV_ONESHOT),
                    fflags: UInt32(NOTE_EXIT), data: 0, udata: nil)
    if kevent(kq, &ev, 1, nil, 0, nil) < 0 {
        action()  // ESRCH: it is already gone
        return true
    }
    Thread.detachNewThread {
        var out = kevent()
        while kevent(kq, nil, 0, &out, 1, nil) < 0 && errno == EINTR {}
        action()
    }
    return true
}

func watchdog(_ parent: pid_t, _ child: pid_t) -> Never {
    let kq = kqueue()
    var events = [
        kevent(ident: UInt(parent), filter: Int16(EVFILT_PROC), flags: UInt16(EV_ADD | EV_ONESHOT),
               fflags: UInt32(NOTE_EXIT), data: 0, udata: nil),
        kevent(ident: UInt(child), filter: Int16(EVFILT_PROC), flags: UInt16(EV_ADD | EV_ONESHOT),
               fflags: UInt32(NOTE_EXIT), data: 0, udata: nil),
    ]
    if kevent(kq, &events[1], 1, nil, 0, nil) < 0 { exit(0) }  // child already gone
    if kevent(kq, &events[0], 1, nil, 0, nil) < 0 {  // parent already gone
        kill(child, SIGKILL)
        exit(0)
    }
    var out = kevent()
    while true {
        let n = kevent(kq, nil, 0, &out, 1, nil)
        if n < 0 && errno == EINTR { continue }
        if n > 0 && out.ident == UInt(parent) { kill(child, SIGKILL) }
        exit(0)
    }
}

// MARK: - main

func listDevices() {
    var inputs: [[String: Any]] = []
    for dev in allDevices() where channelCount(dev, kAudioObjectPropertyScopeInput) > 0 {
        guard let uid = readString(dev, kAudioDevicePropertyDeviceUID) else { continue }
        // Hide other apps' private plumbing (our own tap device is private anyway)
        let transport = readValue(dev, kAudioDevicePropertyTransportType, UInt32(0)) ?? 0
        if transport == kAudioDeviceTransportTypeAggregate && uid.hasPrefix("com.haoawake.") { continue }
        inputs.append(["uid": uid, "name": deviceName(dev)])
    }
    let defIn = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    let defOut = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
    let v = ProcessInfo.processInfo.operatingSystemVersion
    let info: [String: Any] = [
        "inputs": inputs,
        "default_input": readString(defIn, kAudioDevicePropertyDeviceUID) ?? "",
        "default_input_name": defIn != AudioObjectID(kAudioObjectUnknown) ? deviceName(defIn) : "",
        "default_output_name": defOut != AudioObjectID(kAudioObjectUnknown) ? deviceName(defOut) : "",
        "tap_supported": tapSupported,
        "tap_permission": tapSupported ? TCC.statusName : "unsupported",
        "mic_permission": micStatusName(),
        "macos": "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)",
    ]
    if let data = try? JSONSerialization.data(withJSONObject: info, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    }
}

let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "devices":
    listDevices()
case "watchdog":
    guard args.count == 3, let parent = pid_t(args[1]), let child = pid_t(args[2]) else {
        FileHandle.standardError.write("usage: lt-audio watchdog PPID PID\n".data(using: .utf8)!)
        exit(1)
    }
    watchdog(parent, child)
case "tap", "mic":
    signal(SIGPIPE, SIG_DFL)
    let parent = getppid()
    _ = onExit(of: parent) { exit(0) }
    let mode: Capture.Mode = args[0] == "tap" ? .tap : .mic(args.count > 1 && !args[1].hasPrefix("-") ? args[1] : "default")
    let capture = Capture(mode: mode, noRequest: args.contains("--no-request"))
    capture.run()
    withExtendedLifetime(capture) { dispatchMain() }
default:
    FileHandle.standardError.write("usage: lt-audio devices | tap [--no-request] | mic [UID|default] | watchdog PPID PID\n"
        .data(using: .utf8)!)
    exit(1)
}
