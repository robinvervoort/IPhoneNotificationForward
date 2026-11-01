import Foundation

/// Wire protocol between the iPhone extensions and the Home Assistant bridge.
/// Keep in sync with `addon/deskbot_notify/app/protocol.py`.
enum BridgeProtocol {
    static let serviceUUID = "46B6C6E3-47D4-4FD7-98B7-D41D1109B69B"
    static let rxUUID = "297E3C45-E532-40FD-8303-0C6E857AA0F2" // write: iPhone -> bridge
    static let txUUID = "67334EA1-143C-4268-9A63-E34642C7C8DB" // notify: bridge -> iPhone

    enum Suite: UInt8 {
        case xWing = 0
        case p256 = 1
    }

    enum DataKind: UInt8 {
        case ciphertext = 1
        case plaintext = 2
    }

    private static let start: UInt8 = 0x80
    private static let end: UInt8 = 0x40
    private static let seqMask: UInt8 = 0x3F

    // MARK: Messages iPhone -> bridge

    static func getPublicKey(_ suite: Suite) -> Data {
        Data([0x01, suite.rawValue])
    }

    static func keyExchange(suite: Suite, identifier: String, encapsulatedKey: Data) -> Data {
        let ident = Data(identifier.utf8)
        var d = Data([0x02, suite.rawValue, UInt8(ident.count >> 8), UInt8(ident.count & 0xFF)])
        d.append(ident)
        d.append(encapsulatedKey)
        return d
    }

    static func data(kind: DataKind, sessionID: UUID, payload: Data) -> Data {
        var d = Data([0x03, kind.rawValue])
        withUnsafeBytes(of: sessionID.uuid) { d.append(contentsOf: $0) }
        d.append(payload)
        return d
    }

    // MARK: Messages bridge -> iPhone

    static func parsePublicKey(_ message: Data) throws -> (Suite, Data) {
        let bytes = [UInt8](message)
        try throwIfError(bytes)
        guard bytes.count > 2, bytes[0] == 0x81, let suite = Suite(rawValue: bytes[1]) else {
            throw BridgeError.badResponse
        }
        return (suite, Data(bytes[2...]))
    }

    static func checkResult(_ message: Data) throws {
        let bytes = [UInt8](message)
        try throwIfError(bytes)
        guard bytes.count >= 2, bytes[0] == 0x82 else { throw BridgeError.badResponse }
    }

    private static func throwIfError(_ bytes: [UInt8]) throws {
        if bytes.count >= 2, bytes[0] == 0x82, bytes[1] != 0 {
            throw BridgeError.bridge(String(decoding: bytes[2...], as: UTF8.self))
        }
    }

    // MARK: Framing

    static func chunk(_ message: Data, maxPacket: Int) -> [Data] {
        let size = max(maxPacket - 1, 1)
        var packets: [Data] = []
        var offset = message.startIndex
        var seq: UInt8 = 0
        repeat {
            let next = min(offset + size, message.endIndex)
            var header = seq & seqMask
            if offset == message.startIndex { header |= start }
            if next == message.endIndex { header |= end }
            var packet = Data([header])
            packet.append(message[offset..<next])
            packets.append(packet)
            offset = next
            seq &+= 1
        } while offset < message.endIndex
        return packets
    }

    struct Reassembler {
        private var buffer: Data?
        private var nextSeq: UInt8 = 0

        mutating func feed(_ packet: Data) -> Data? {
            guard let header = packet.first else { return nil }
            let body = packet.dropFirst()
            if header & BridgeProtocol.start != 0 {
                buffer = Data()
                nextSeq = 0
            }
            guard buffer != nil, header & BridgeProtocol.seqMask == nextSeq else {
                buffer = nil
                return nil
            }
            buffer!.append(body)
            nextSeq = (nextSeq &+ 1) & BridgeProtocol.seqMask
            if header & BridgeProtocol.end != 0 {
                defer { buffer = nil }
                return buffer
            }
            return nil
        }
    }
}

enum BridgeError: LocalizedError {
    case notPaired
    case bluetoothUnavailable
    case peripheralNotFound
    case serviceNotFound
    case disconnected
    case timeout
    case badResponse
    case noWebhook
    case http(Int)
    case bridge(String)

    var errorDescription: String? {
        switch self {
        case .notPaired: "The bridge is not paired yet."
        case .bluetoothUnavailable: "Bluetooth is unavailable."
        case .peripheralNotFound: "The bridge is not known to this iPhone."
        case .serviceNotFound: "The bridge service was not found."
        case .disconnected: "The bridge disconnected."
        case .timeout: "The bridge did not answer in time."
        case .badResponse: "Unexpected response from the bridge."
        case .noWebhook: "No Home Assistant webhook URL is configured."
        case .http(let code): "Home Assistant returned HTTP \(code)."
        case .bridge(let message): "Bridge error: \(message)"
        }
    }
}
