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
        let line = Data("\(stamp) [\(source)] \(message)\n".utf8)
        // Append-only with O_APPEND: the app and each extension are separate
        // processes, and a read-modify-write would let them overwrite each other.
        lock.lock()
        defer { lock.unlock() }
        let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        guard fd >= 0 else { return }
        _ = line.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        close(fd)
    }

    /// Returns the log and trims it to the newest lines (called from the app only).
    static func read() -> String {
        guard let url = fileURL else { return "(app group unavailable)" }
        let text = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        let lines = text.split(separator: "\n", omittingEmptySubsequences: true)
        guard lines.count > maxLines else { return text }
        let trimmed = lines.suffix(maxLines).joined(separator: "\n") + "\n"
        try? trimmed.write(to: url, atomically: true, encoding: .utf8)
        return trimmed
    }

    static func clear() {
        guard let url = fileURL else { return }
        try? FileManager.default.removeItem(at: url)
    }
}
