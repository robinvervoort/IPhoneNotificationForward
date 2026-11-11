import Foundation
import os

/// Small shared log so the extensions' activity (each runs in its own process)
/// can be read inside the app.
///
/// Stored in the app group's shared UserDefaults rather than a file: the
/// extensions can read and write the shared settings (the key exchange relies
/// on them), but their writes to a file in the group container never showed up.
/// Each process keeps its own list, so processes never overwrite each other;
/// the app merges them by time when displaying.
enum DiagnosticLog {
    private static let maxPerSource = 150
    private static let keyPrefix = "diag."
    private static let lock = NSLock()
    private static let logger = Logger(subsystem: "be.robinvervoort.notificationforward", category: "diag")

    private static var defaults: UserDefaults? { UserDefaults(suiteName: SharedSettings.appGroup) }

    /// Short name of the running process: notificationforward, dataprovider, transportsecurity, transport.
    private static let source: String = {
        let id = Bundle.main.bundleIdentifier ?? "?"
        return id.split(separator: ".").last.map(String.init) ?? id
    }()

    static func log(_ message: String) {
        logger.log("\(message, privacy: .public)")
        guard let defaults else { return }
        let now = Date()
        let stamp = ISO8601DateFormatter.string(from: now, timeZone: .current,
                                                formatOptions: [.withTime, .withColonSeparatorInTime])
        // "<epoch>|<display line>": the epoch is only used for sorting.
        let entry = "\(now.timeIntervalSince1970)|\(stamp) [\(source)] \(message)"
        lock.lock()
        defer { lock.unlock() }
        let key = keyPrefix + source
        var lines = defaults.stringArray(forKey: key) ?? []
        lines.append(entry)
        if lines.count > maxPerSource { lines.removeFirst(lines.count - maxPerSource) }
        defaults.set(lines, forKey: key)
    }

    /// All processes' lines, oldest first.
    static func read() -> String {
        guard let defaults else { return "(app group unavailable)" }
        let entries = defaults.dictionaryRepresentation()
            .filter { $0.key.hasPrefix(keyPrefix) }
            .flatMap { ($0.value as? [String]) ?? [] }
        let sorted = entries.compactMap { entry -> (Double, Substring)? in
            guard let bar = entry.firstIndex(of: "|"), let t = Double(entry[..<bar]) else { return nil }
            return (t, entry[entry.index(after: bar)...])
        }.sorted { $0.0 < $1.0 }
        return sorted.map { String($0.1) }.joined(separator: "\n")
    }

    static func clear() {
        guard let defaults else { return }
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(keyPrefix) {
            defaults.removeObject(forKey: key)
        }
    }
}
