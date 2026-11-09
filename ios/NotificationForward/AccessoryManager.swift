import AccessoryNotifications
import AccessorySetupKit
import CoreBluetooth
import Observation
import UIKit

/// Pairs the Home Assistant bridge with AccessorySetupKit and manages
/// notification forwarding permission.
@MainActor
@Observable
final class AccessoryManager {
    private(set) var accessory: ASAccessory?
    private(set) var status = "Not paired"
    var lastError: String?

    private let session = ASAccessorySession()
    private let center = AccessoryNotificationCenter()

    /// The app keeps its own Bluetooth connection to the bridge open (like the
    /// Pebble app does with its watch); iOS appears to forward only to an
    /// accessory that is actually connected.
    private var link: BLELink?
    private var linkID: UUID?
    private var keepAlive: Task<Void, Never>?

    func activate() {
        DiagnosticLog.log("App start, iOS \(UIDevice.current.systemVersion)")
        session.activate(on: .main) { [weak self] event in
            Task { @MainActor in self?.handle(event) }
        }
    }

    private func handle(_ event: ASAccessoryEvent) {
        switch event.eventType {
        case .activated:
            setAccessory(session.accessories.first)
        case .accessoryAdded, .accessoryChanged:
            setAccessory(event.accessory ?? session.accessories.first)
        case .accessoryRemoved:
            setAccessory(nil)
        default:
            break
        }
        if let error = event.error {
            lastError = error.localizedDescription
        }
    }

    private func setAccessory(_ accessory: ASAccessory?) {
        self.accessory = accessory
        SharedSettings.accessoryBluetoothID = accessory?.bluetoothIdentifier
        DiagnosticLog.log("Accessory: \(accessory?.displayName ?? "none"), BT id \(accessory?.bluetoothIdentifier?.uuidString.prefix(8) ?? "nil")")
        updateLink(for: accessory?.bluetoothIdentifier)
        Task { await refreshStatus() }
    }

    private func updateLink(for id: UUID?) {
        guard id != linkID else { return }
        keepAlive?.cancel()
        link?.disconnect()
        link = nil
        linkID = id
        guard let id else { return }
        let link = BLELink(peripheralID: id, deviceAccess: false)
        self.link = link
        keepAlive = Task {
            var wasConnected = false
            while !Task.isCancelled {
                do {
                    try await link.connect(timeout: 30)
                    if !wasConnected { DiagnosticLog.log("App: connected to bridge") }
                    wasConnected = true
                } catch {
                    if wasConnected { DiagnosticLog.log("App: lost bridge connection: \(error.localizedDescription)") }
                    wasConnected = false
                }
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    // MARK: - Actions

    func pair() {
        let descriptor = ASDiscoveryDescriptor()
        descriptor.bluetoothServiceUUID = CBUUID(string: BridgeProtocol.serviceUUID)
        // iOS only forwards notifications to an accessory it has a secure, bonded
        // LE link with; without this the request fails with .unsupportedAccessory.
        descriptor.supportedOptions = .bluetoothPairingLE
        let image = UIImage(systemName: "house.and.flag.fill") ?? UIImage()
        let item = ASPickerDisplayItem(name: "Notification Forward Bridge", productImage: image, descriptor: descriptor)
        session.showPicker(for: [item]) { [weak self] error in
            if let error {
                Task { @MainActor in self?.lastError = error.localizedDescription }
            }
        }
    }

    func unpair() {
        guard let accessory else { return }
        session.removeAccessory(accessory) { [weak self] error in
            Task { @MainActor in
                if let error { self?.lastError = error.localizedDescription }
            }
        }
    }

    func requestForwarding() async {
        guard let accessory else { return }
        do {
            let decision = try await center.requestForwarding(for: accessory)
            status = "Forwarding: \(decision)"
            DiagnosticLog.log("requestForwarding: \(decision)")
        } catch {
            DiagnosticLog.log("requestForwarding failed: \(error)")
            lastError = error.localizedDescription
        }
        await refreshStatus()
    }

    func refreshStatus() async {
        guard let accessory else {
            status = "Not paired"
            return
        }
        do {
            let current = try await center.forwardingStatus(for: accessory)
            status = "Forwarding: \(current)"
        } catch {
            status = "Paired (forwarding status unavailable)"
        }
    }
}
