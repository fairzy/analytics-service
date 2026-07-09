import Foundation
import UIKit
import CryptoKit
import Security
import os

/// 轻量埋点客户端：直连 analytics-service，失败静默，不影响主流程。
///
/// 接入：
/// ```swift
/// AnalyticsClient.install(AnalyticsConfig(
///   appName: "liveai",
///   payloadKeyHex: "<64 hex or nil>",
///   keychainService: "ai.talent.liveai.analytics"
/// ))
/// await AnalyticsClient.shared.track("app_open")
/// ```
///
/// - **clientId**：Keychain 持久 UUID（重启/卸载重装尽量不变）
/// - **加密**：配置了 `payloadKeyHex` 则 AES-256-GCM 信封，否则明文 JSON
public actor AnalyticsClient {
    public static let shared = AnalyticsClient()

    private var buffer: [PendingEvent] = []
    private var flushTask: Task<Void, Never>?
    private var userId: String?
    private var cachedClientId: String?

    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private let encoder = JSONEncoder()
    private let log = Logger(subsystem: "AnalyticsKit", category: "client")

    private struct PendingEvent: Encodable, Sendable {
        let event: String
        let ts: String
        let props: [String: AnalyticsValue]?
    }

    private struct Environment: Sendable {
        let clientId: String
        let appVersion: String?
        let osVersion: String
        let locale: String
        let deviceModel: String
    }

    // MARK: - Bootstrap

    /// 启动时调用一次（须在任何 track / Keychain 读之前，且可同步）。
    public nonisolated static func install(_ config: AnalyticsConfig) {
        AnalyticsRuntime.config = config
    }

    /// 是否已 install。
    public nonisolated static var isInstalled: Bool {
        AnalyticsRuntime.config != nil
    }

    // MARK: - Public API

    public func setUserId(_ id: String?) {
        userId = id
    }

    public func track(_ event: String, props: [String: AnalyticsValue] = [:]) {
        guard AnalyticsRuntime.config != nil else {
            log.warning("Analytics track ignored — not installed. Call AnalyticsClient.install first.")
            return
        }
        let name = event.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        let config = AnalyticsRuntime.requireConfig()
        guard propsWithinLimit(props, maxBytes: config.maxPropsBytes) else {
            log.warning("Analytics drop event props too large — event:\(name, privacy: .public)")
            return
        }

        if buffer.count >= config.maxBufferSize {
            buffer.removeFirst(buffer.count - config.maxBufferSize + 1)
        }
        buffer.append(PendingEvent(
            event: name,
            ts: iso.string(from: Date()),
            props: props.isEmpty ? nil : props
        ))
        scheduleFlush(delay: config.flushDelayNanos)
    }

    public func flush() async {
        guard !buffer.isEmpty else { return }
        let config = AnalyticsRuntime.requireConfig()

        let count = min(config.maxBatchSize, buffer.count)
        let events = Array(buffer.prefix(count))
        buffer.removeFirst(count)

        struct Body: Encodable {
            let app_name: String
            let device_id: String
            let app_version: String?
            let os_version: String
            let locale: String
            let user_id: String?
            let events: [PendingEvent]
        }

        let env = await Self.currentEnvironment(clientId: resolveClientId(), config: config)
        let decoratedEvents = events.map { event in
            var props = event.props ?? [:]
            props["device_model"] = .string(env.deviceModel)
            return PendingEvent(event: event.event, ts: event.ts, props: props)
        }
        let body = Body(
            app_name: config.appName,
            device_id: env.clientId,
            app_version: env.appVersion,
            os_version: env.osVersion,
            locale: env.locale,
            user_id: userId,
            events: decoratedEvents
        )

        var req = URLRequest(url: config.trackURL)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 10

        do {
            let plain = try encoder.encode(body)
            if let keyHex = config.payloadKeyHex, !keyHex.isEmpty {
                req.httpBody = try Self.encryptEnvelope(plain: plain, appName: config.appName, keyHex: keyHex, encoder: encoder)
            } else {
                req.httpBody = plain
            }
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                let text = String(data: data, encoding: .utf8) ?? "<binary>"
                log.warning("Analytics track HTTP \(http.statusCode): \(text.prefix(200), privacy: .public)")
            }
        } catch {
            log.debug("Analytics track failed silently: \(String(describing: error), privacy: .public)")
        }

        if !buffer.isEmpty {
            scheduleFlush(delay: config.flushDelayNanos)
        }
    }

    // MARK: - Client ID (Keychain)

    private func resolveClientId() -> String {
        if let cachedClientId, !cachedClientId.isEmpty { return cachedClientId }
        let id = Self.loadOrCreateClientId()
        cachedClientId = id
        return id
    }

    /// 读 Keychain clientId。App 冷启动 purge Keychain 前可先读出再写回。
    public nonisolated static func readKeychainClientId() -> String? {
        guard let config = AnalyticsRuntime.config else { return nil }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: config.keychainService,
            kSecAttrAccount as String: config.keychainAccount,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data,
              let id = String(data: data, encoding: .utf8), !id.isEmpty else {
            return nil
        }
        return id
    }

    /// 写入/覆盖 Keychain clientId。
    public nonisolated static func writeKeychainClientId(_ id: String) {
        guard let config = AnalyticsRuntime.config else { return }
        let deleteQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: config.keychainService,
            kSecAttrAccount as String: config.keychainAccount,
        ]
        SecItemDelete(deleteQuery as CFDictionary)

        guard let data = id.data(using: .utf8) else { return }
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: config.keychainService,
            kSecAttrAccount as String: config.keychainAccount,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        SecItemAdd(add as CFDictionary, nil)
    }

    public nonisolated static func loadOrCreateClientId() -> String {
        if let existing = readKeychainClientId() {
            return existing
        }

        let config = AnalyticsRuntime.requireConfig()
        let defaults = UserDefaults.standard
        if let legacyKey = config.legacyInstallIdDefaultsKey,
           let legacy = defaults.string(forKey: legacyKey), !legacy.isEmpty {
            writeKeychainClientId(legacy)
            return legacy
        }

        let id = UUID().uuidString.lowercased()
        writeKeychainClientId(id)
        if let legacyKey = config.legacyInstallIdDefaultsKey {
            defaults.set(id, forKey: legacyKey)
        }
        return id
    }

    // MARK: - Encryption

    private static func encryptEnvelope(
        plain: Data,
        appName: String,
        keyHex: String,
        encoder: JSONEncoder
    ) throws -> Data {
        guard let keyData = Data(analyticsHexString: keyHex), keyData.count == 32 else {
            throw AnalyticsCryptoError.invalidKey
        }
        let key = SymmetricKey(data: keyData)
        let sealed = try AES.GCM.seal(plain, using: key)
        guard let combined = sealed.combined else {
            throw AnalyticsCryptoError.sealFailed
        }

        struct Envelope: Encodable {
            let v: Int
            let app_name: String
            let enc: String
            let data: String
        }
        return try encoder.encode(Envelope(
            v: 1,
            app_name: appName,
            enc: "aes-256-gcm",
            data: combined.base64EncodedString()
        ))
    }

    // MARK: - Internals

    private func propsWithinLimit(_ props: [String: AnalyticsValue], maxBytes: Int) -> Bool {
        guard !props.isEmpty else { return true }
        guard let data = try? encoder.encode(props) else { return false }
        return data.count <= maxBytes
    }

    private func scheduleFlush(delay: UInt64) {
        flushTask?.cancel()
        flushTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            if Task.isCancelled { return }
            await self?.flush()
        }
    }

    @MainActor
    private static func currentEnvironment(clientId: String, config: AnalyticsConfig) -> Environment {
        Environment(
            clientId: clientId,
            appVersion: config.appVersionProvider(),
            osVersion: UIDevice.current.systemVersion,
            locale: Locale.current.identifier,
            deviceModel: config.deviceModelProvider()
        )
    }
}

private enum AnalyticsCryptoError: Error {
    case invalidKey
    case sealFailed
}

private extension Data {
    init?(analyticsHexString: String) {
        let cleaned = analyticsHexString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard cleaned.count % 2 == 0, !cleaned.isEmpty else { return nil }
        var data = Data(capacity: cleaned.count / 2)
        var index = cleaned.startIndex
        while index < cleaned.endIndex {
            let next = cleaned.index(index, offsetBy: 2)
            guard let byte = UInt8(cleaned[index..<next], radix: 16) else { return nil }
            data.append(byte)
            index = next
        }
        self = data
    }
}
