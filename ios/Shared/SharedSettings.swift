import Foundation

/// Settings shared between the app and its extensions through the app group.
enum SharedSettings {
    static let appGroup = "group.be.robinvervoort.notificationforward"

    private static var defaults: UserDefaults {
        UserDefaults(suiteName: appGroup) ?? .standard
    }

    /// CoreBluetooth identifier of the bridge (from `ASAccessory.bluetoothIdentifier`).
    static var accessoryBluetoothID: UUID? {
        get { defaults.string(forKey: "accessoryBluetoothID").flatMap(UUID.init(uuidString:)) }
        set { defaults.set(newValue?.uuidString, forKey: "accessoryBluetoothID") }
    }

    /// Full Home Assistant webhook URL used for internet deliveries.
    static var webhookURL: URL? {
        get { defaults.string(forKey: "webhookURL").flatMap(URL.init(string:)) }
        set { defaults.set(newValue?.absoluteString, forKey: "webhookURL") }
    }
}
