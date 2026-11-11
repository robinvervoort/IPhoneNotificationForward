import CoreBluetooth
import Foundation

/// Minimal async CoreBluetooth central that talks to the bridge.
///
/// All CoreBluetooth state lives on `queue`; public async methods hop onto it.
final class BLELink: NSObject, @unchecked Sendable {
    private let peripheralID: UUID
    private let queue = DispatchQueue(label: "be.robinvervoort.notificationforward.ble")
    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var rx: CBCharacteristic?
    private var tx: CBCharacteristic?
    private var reassembler = BridgeProtocol.Reassembler()

    private var readyWaiters: [Int: CheckedContinuation<Void, Error>] = [:]
    private var writeWaiter: (token: Int, continuation: CheckedContinuation<Void, Error>, quiet: Bool)?
    private var responseWaiter: (token: Int, continuation: CheckedContinuation<Data, Error>)?
    private var nextToken = 0
    private var isScanning = false
    private var serviceUUID: CBUUID { CBUUID(string: BridgeProtocol.serviceUUID) }
    private var isReady: Bool { peripheral?.state == .connected && rx != nil && tx?.isNotifying == true }
    private let sendGate = SendGate()
    private var pendingRemote: [String] = []
    private var rxPackets = 0
    private var rxLargest = 0

    /// `deviceAccess` adds CBCentralManagerOptionDeviceAccessForMedia, which the
    /// extensions use (as in the working Pebble code); the app must not set it,
    /// nor a restore identifier, or iOS reports the central as unsupported.
    init(peripheralID: UUID, restoreIdentifier: String? = nil, deviceAccess: Bool = true) {
        self.peripheralID = peripheralID
        super.init()
        // DeviceAccessForMedia lets an extension use the AccessorySetupKit-approved
        // accessory (as the working Pebble implementation does).
        var options: [String: Any] = deviceAccess ? [CBCentralManagerOptionDeviceAccessForMedia: true] : [:]
        if let restoreIdentifier {
            options[CBCentralManagerOptionRestoreIdentifierKey] = restoreIdentifier
        }
        DiagnosticLog.log("BLE: central created for \(peripheralID.uuidString.prefix(8)), restore=\(restoreIdentifier != nil)")
        central = CBCentralManager(delegate: self, queue: queue, options: options)
    }

    /// Connects (if needed), discovers the bridge service and subscribes to notifications.
    func connect(timeout: TimeInterval = 15) async throws {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            self.queue.async {
                if self.isReady { c.resume(); return }
                let token = self.makeToken()
                self.readyWaiters[token] = c
                self.queue.asyncAfter(deadline: .now() + timeout) {
                    self.readyWaiters.removeValue(forKey: token)?.resume(throwing: BridgeError.timeout)
                }
                self.advance()
            }
        }
    }

    /// Sends one logical message, chunked to the negotiated MTU, each chunk acknowledged.
    /// Messages are sent one at a time: there is a single write-acknowledgement slot.
    func send(_ message: Data, quiet: Bool = false) async throws {
        await sendGate.acquire()
        do {
            try await sendNow(message, quiet: quiet)
            await sendGate.release()
        } catch {
            await sendGate.release()
            throw error
        }
    }

    /// Forwards a diagnostics line to the bridge; buffered until the link is ready.
    func remoteLog(_ text: String) {
        queue.async {
            guard self.isReady else {
                if self.pendingRemote.count < 40 { self.pendingRemote.append(text) }
                return
            }
            Task { try? await self.send(BridgeProtocol.log(text), quiet: true) }
        }
    }

    private func flushRemote() {
        let lines = pendingRemote
        pendingRemote.removeAll()
        guard !lines.isEmpty else { return }
        Task {
            for line in lines { try? await self.send(BridgeProtocol.log(line), quiet: true) }
        }
    }

    private func sendNow(_ message: Data, quiet: Bool) async throws {
        try await connect()
        let maxLen = queue.sync { peripheral?.maximumWriteValueLength(for: .withResponse) ?? 20 }
        for packet in BridgeProtocol.chunk(message, maxPacket: maxLen) {
            try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
                self.queue.async {
                    guard let p = self.peripheral, let rx = self.rx else {
                        c.resume(throwing: BridgeError.disconnected); return
                    }
                    let token = self.makeToken()
                    self.writeWaiter = (token, c, quiet)
                    self.queue.asyncAfter(deadline: .now() + 10) {
                        guard let w = self.writeWaiter, w.token == token else { return }
                        self.writeWaiter = nil
                        w.continuation.resume(throwing: BridgeError.timeout)
                    }
                    p.writeValue(packet, for: rx, type: .withResponse)
                }
            }
        }
    }

    /// Sends a message and waits for the bridge's reply.
    func request(_ message: Data, timeout: TimeInterval = 15) async throws -> Data {
        try await connect()
        return try await withCheckedThrowingContinuation { (c: CheckedContinuation<Data, Error>) in
            self.queue.async {
                self.responseWaiter?.continuation.resume(throwing: BridgeError.badResponse)
                let token = self.makeToken()
                self.responseWaiter = (token, c)
                self.queue.asyncAfter(deadline: .now() + timeout) {
                    guard let w = self.responseWaiter, w.token == token else { return }
                    self.responseWaiter = nil
                    w.continuation.resume(throwing: BridgeError.timeout)
                }
                Task { do { try await self.send(message) } catch { self.failResponse(error) } }
            }
        }
    }

    func disconnect() {
        queue.async {
            if let p = self.peripheral { self.central.cancelPeripheralConnection(p) }
        }
    }

    // MARK: - State machine (queue only)

    private func advance() {
        guard central.state == .poweredOn else {
            if central.state == .unsupported || central.state == .unauthorized {
                DiagnosticLog.log("BLE: unavailable (state \(central.state.rawValue))")
                failAll(BridgeError.bluetoothUnavailable)
            }
            return // wait for centralManagerDidUpdateState
        }
        if peripheral == nil {
            // 1. the identifier AccessorySetupKit gave the app, 2. an already
            // connected bridge, 3. scan for the bridge's service.
            let known = central.retrievePeripherals(withIdentifiers: [peripheralID])
            let connected = central.retrieveConnectedPeripherals(withServices: [serviceUUID])
            DiagnosticLog.log("BLE: retrieve known=\(known.count) connected=\(connected.count)")
            guard let p = known.first ?? connected.first else {
                if !isScanning {
                    isScanning = true
                    DiagnosticLog.log("BLE: scanning for bridge service")
                    central.scanForPeripherals(withServices: [serviceUUID])
                }
                return
            }
            peripheral = p
            p.delegate = self
        }
        guard let p = peripheral else { return }
        switch p.state {
        case .disconnected:
            DiagnosticLog.log("BLE: connecting to \(p.identifier.uuidString.prefix(8))")
            central.connect(p)
        case .connected:
            if rx == nil || tx == nil {
                p.discoverServices([CBUUID(string: BridgeProtocol.serviceUUID)])
            } else if tx?.isNotifying != true, let tx {
                p.setNotifyValue(true, for: tx)
            } else {
                let waiters = readyWaiters.values
                readyWaiters.removeAll()
                if !waiters.isEmpty {
                    DiagnosticLog.log("BLE: ready, write max \(p.maximumWriteValueLength(for: .withResponse)), "
                                      + "packet max \(p.maximumWriteValueLength(for: .withoutResponse))")
                }
                waiters.forEach { $0.resume() }
                flushRemote()
            }
        default:
            break
        }
    }

    private func failAll(_ error: Error) {
        DiagnosticLog.log("BLE: failed: \(error.localizedDescription)")
        let waiters = readyWaiters.values
        readyWaiters.removeAll()
        waiters.forEach { $0.resume(throwing: error) }
        writeWaiter?.continuation.resume(throwing: error)
        writeWaiter = nil
        responseWaiter?.continuation.resume(throwing: error)
        responseWaiter = nil
    }

    private func failResponse(_ error: Error) {
        queue.async {
            self.responseWaiter?.continuation.resume(throwing: error)
            self.responseWaiter = nil
        }
    }

    private func makeToken() -> Int {
        nextToken += 1
        return nextToken
    }
}

extension BLELink: CBCentralManagerDelegate, CBPeripheralDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        DiagnosticLog.log("BLE: state \(central.state.rawValue) (5 = on, 3 = unauthorized)")
        advance()
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard self.peripheral == nil else { return }
        DiagnosticLog.log("BLE: found bridge by scan (\(peripheral.identifier.uuidString.prefix(8)), RSSI \(RSSI))")
        central.stopScan()
        isScanning = false
        self.peripheral = peripheral
        peripheral.delegate = self
        advance()
    }

    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        if let restored = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?
            .first(where: { $0.identifier == peripheralID }) {
            peripheral = restored
            restored.delegate = self
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        DiagnosticLog.log("BLE: connected")
        advance()
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        failAll(error ?? BridgeError.disconnected)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
        DiagnosticLog.log("BLE: disconnected \(error?.localizedDescription ?? "")")
        rx = nil
        tx = nil
        reassembler = BridgeProtocol.Reassembler()
        failAll(BridgeError.disconnected)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let service = peripheral.services?.first(where: {
            $0.uuid == CBUUID(string: BridgeProtocol.serviceUUID)
        }) else {
            failAll(error ?? BridgeError.serviceNotFound); return
        }
        peripheral.discoverCharacteristics(
            [CBUUID(string: BridgeProtocol.rxUUID), CBUUID(string: BridgeProtocol.txUUID)], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        rx = service.characteristics?.first { $0.uuid == CBUUID(string: BridgeProtocol.rxUUID) }
        tx = service.characteristics?.first { $0.uuid == CBUUID(string: BridgeProtocol.txUUID) }
        guard rx != nil, tx != nil else { failAll(error ?? BridgeError.serviceNotFound); return }
        DiagnosticLog.log("BLE: bridge service ready")
        advance()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                    error: Error?) {
        if let error { failAll(error); return }
        advance()
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        let waiter = writeWaiter?.continuation
        let quiet = writeWaiter?.quiet ?? false
        writeWaiter = nil
        if let error, !quiet { DiagnosticLog.log("BLE: write failed: \(error.localizedDescription)") }
        if let error { waiter?.resume(throwing: error) } else { waiter?.resume() }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == CBUUID(string: BridgeProtocol.txUUID), let value = characteristic.value,
              let header = value.first else { return }
        if header & 0x80 != 0 { rxPackets = 0; rxLargest = 0 }   // START
        rxPackets += 1
        rxLargest = max(rxLargest, value.count)
        guard let message = reassembler.feed(value) else {
            if header & 0x40 != 0 {                                // END without a complete message
                DiagnosticLog.log("BLE: dropped reply: packet \(rxPackets) out of sequence or incomplete "
                                  + "(\(value.count) bytes, largest \(rxLargest))")
            }
            return
        }
        DiagnosticLog.log("BLE: reply 0x\(String(format: "%02x", message.first ?? 0)): \(message.count) bytes "
                          + "in \(rxPackets) packets (largest \(rxLargest))")
        if responseWaiter == nil { DiagnosticLog.log("BLE: reply arrived but nobody was waiting for it") }
        responseWaiter?.continuation.resume(returning: message)
        responseWaiter = nil
    }
}


/// Lets one `send` run at a time (CoreBluetooth gives one write callback per write).
private actor SendGate {
    private var busy = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func acquire() async {
        if !busy {
            busy = true
            return
        }
        await withCheckedContinuation { waiters.append($0) }
    }

    func release() {
        if waiters.isEmpty {
            busy = false
        } else {
            waiters.removeFirst().resume()
        }
    }
}
