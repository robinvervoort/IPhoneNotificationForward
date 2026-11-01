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
    private var writeWaiter: (token: Int, continuation: CheckedContinuation<Void, Error>)?
    private var responseWaiter: (token: Int, continuation: CheckedContinuation<Data, Error>)?
    private var nextToken = 0
    private var isReady: Bool { peripheral?.state == .connected && rx != nil && tx?.isNotifying == true }

    init(peripheralID: UUID, restoreIdentifier: String? = nil) {
        self.peripheralID = peripheralID
        super.init()
        var options: [String: Any] = [:]
        if let restoreIdentifier {
            options[CBCentralManagerOptionRestoreIdentifierKey] = restoreIdentifier
        }
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
    func send(_ message: Data) async throws {
        try await connect()
        let maxLen = queue.sync { peripheral?.maximumWriteValueLength(for: .withResponse) ?? 20 }
        for packet in BridgeProtocol.chunk(message, maxPacket: maxLen) {
            try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
                self.queue.async {
                    guard let p = self.peripheral, let rx = self.rx else {
                        c.resume(throwing: BridgeError.disconnected); return
                    }
                    let token = self.makeToken()
                    self.writeWaiter = (token, c)
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
                failAll(BridgeError.bluetoothUnavailable)
            }
            return // wait for centralManagerDidUpdateState
        }
        if peripheral == nil {
            guard let p = central.retrievePeripherals(withIdentifiers: [peripheralID]).first else {
                failAll(BridgeError.peripheralNotFound); return
            }
            peripheral = p
            p.delegate = self
        }
        guard let p = peripheral else { return }
        switch p.state {
        case .disconnected:
            central.connect(p)
        case .connected:
            if rx == nil || tx == nil {
                p.discoverServices([CBUUID(string: BridgeProtocol.serviceUUID)])
            } else if tx?.isNotifying != true, let tx {
                p.setNotifyValue(true, for: tx)
            } else {
                let waiters = readyWaiters.values
                readyWaiters.removeAll()
                waiters.forEach { $0.resume() }
            }
        default:
            break
        }
    }

    private func failAll(_ error: Error) {
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
        advance()
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        failAll(error ?? BridgeError.disconnected)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
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
        advance()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                    error: Error?) {
        if let error { failAll(error); return }
        advance()
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        let waiter = writeWaiter?.continuation
        writeWaiter = nil
        if let error { waiter?.resume(throwing: error) } else { waiter?.resume() }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == CBUUID(string: BridgeProtocol.txUUID), let value = characteristic.value,
              let message = reassembler.feed(value) else { return }
        responseWaiter?.continuation.resume(returning: message)
        responseWaiter = nil
    }
}
