import Foundation
import os

/// Tiny shared log so the extensions' activity (each runs in its own process)
/// can be read inside the app. Stored in the app group; keeps the last lines.
enum DiagnosticLog {
    private static let maxLines = 300
    private static let lock = NSLock()
    private static let logger = Logger(subsystem: "be.robinvervoort.notificationforward", category: "diag")

    private static var fileURL: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: SharedSettings.appGroup)?
            .appendingPathComponent("diagnostics.log")
    }

    /// Short name of the running process: app, DataProvider, TransportSecurity, Transport.
    private static let source: String = {
        let id = Bundle.main.bundleIdentifier ?? "?"
        return id.split(separator: ".").last.map(String.init) ?? id
    }()

    static func log(_ message: String) {
        logger.log("\(message, privacy: .public)")
        guard let url = fileURL else { return }
        let stamp = ISO8601DateFormatter.string(from: Date(), timeZone: .current,
                                                formatOptions: [.withTime, .withColonSeparatorInTime])
        let line = "\(stamp) [\(source)] \(message)\n"
        lock.lock()
        defer { lock.unlock() }
        var text = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        text += line
        let lines = text.split(separator: "\n", omittingEmptySubsequences: true)
        if lines.count > maxLines {
            text = lines.suffix(maxLines).joined(separator: "\n") + "\n"
        }
        try? text.write(to: url, atomically: true, encoding: .utf8)
    }

    static func read() -> String {
        guard let url = fileURL else { return "(app group unavailable)" }
        return (try? String(contentsOf: url, encoding: .utf8)) ?? ""
    }

    static func clear() {
        guard let url = fileURL else { return }
        try? FileManager.default.removeItem(at: url)
    }
}
