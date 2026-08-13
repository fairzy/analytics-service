import Foundation
import UIKit
import CryptoKit
import Security
import Network
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
/// - **中国大陆网络权限**：启动后系统弹「允许无线数据」期间请求会失败；
///   失败事件会放回 buffer，并用 `NWPathMonitor` 在恢复联网后自动 flush。
public actor AnalyticsClient {
    public static let shared = AnalyticsClient()

    private var buffer: [PendingEvent] = []
    private var flushTask: Task<Void, Never>?
    private var userId: String?
    private var cachedClientId: String?

    /// 待发队列是否已从磁盘读回（懒加载，首次 track / flush 时触发）。
    private var didLoadPersisted = false

    /// 是否正在发送，避免并发 flush 打乱 requeue 顺序。
    private var isFlushing = false
    /// 连续失败次数，用于退避；成功后清零。
    private var consecutiveFailures = 0
    /// 当前是否可达（乐观初始 true，首个 path 回调校正）。
    private var isOnline = true
    private var networkMonitorStarted = false
    private var pathMonitor: NWPathMonitor?

    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private let encoder = JSONEncoder()
    private let log = Logger(subsystem: "AnalyticsKit", category: "client")

    private struct PendingEvent: Codable, Sendable {
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
        ensureNetworkMonitor()
        ensureLoaded()

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
        persist()
        scheduleFlush(delay: config.flushDelayNanos)
    }

    public func flush() async {
        guard AnalyticsRuntime.config != nil else { return }
        ensureNetworkMonitor()
        ensureLoaded()
        guard !buffer.isEmpty, !isFlushing else { return }

        isFlushing = true
        defer { isFlushing = false }

        let config = AnalyticsRuntime.requireConfig()
        let count = min(config.maxBatchSize, buffer.count)
        let events = Array(buffer.prefix(count))
        // 先取出再发：成功则丢弃，失败放回队头，避免中国区网络权限窗口期丢事件。
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
                requeue(events, config: config)
                scheduleRetryIfNeeded(config: config)
                return
            }
            // 成功：已发出去的不必再留在磁盘上
            consecutiveFailures = 0
            persist()
            if !buffer.isEmpty {
                scheduleFlush(delay: config.flushDelayNanos)
            }
        } catch {
            if Self.isCancellation(error) {
                // 兜底：scheduleFlush 修好后本不该再出现，但若发送任务因其它原因被取消
                // （如 App 挂起），放回队头并按正常节奏重试即可，不涨退避。
                log.debug("Analytics flush cancelled, requeueing without backoff")
                requeue(events, config: config, countAsFailure: false)
                scheduleFlush(delay: config.flushDelayNanos)
                return
            }
            log.debug("Analytics track failed silently: \(String(describing: error), privacy: .public)")
            requeue(events, config: config)
            scheduleRetryIfNeeded(config: config)
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

    // MARK: - Network recovery (中国大陆「允许无线数据」)

    /// 监听路径变化：用户点允许后 path 由 unsatisfied → satisfied，自动重试 buffer。
    private func ensureNetworkMonitor() {
        guard !networkMonitorStarted else { return }
        networkMonitorStarted = true
        let monitor = NWPathMonitor()
        pathMonitor = monitor
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            Task { await self?.handlePathUpdate(online: online) }
        }
        monitor.start(queue: DispatchQueue(label: "AnalyticsKit.network"))
    }

    private func handlePathUpdate(online: Bool) {
        let recovered = online && !isOnline
        isOnline = online
        if recovered {
            log.info("Analytics network recovered — flushing buffered events")
            consecutiveFailures = 0
            Task { await flush() }
        } else if !online {
            log.debug("Analytics network unavailable (e.g. awaiting cellular data permission)")
        }
    }

    /// 放回队头。`countAsFailure = false` 用于任务取消这类"非真实失败"，
    /// 避免把它计进指数退避。
    private func requeue(_ events: [PendingEvent], config: AnalyticsConfig, countAsFailure: Bool = true) {
        buffer = events + buffer
        if buffer.count > config.maxBufferSize {
            buffer = Array(buffer.suffix(config.maxBufferSize))
        }
        if countAsFailure { consecutiveFailures += 1 }
        // 发送失败的事件必须落盘：网络故障期正是最需要留痕的时段，
        // 只留在内存里的话 App 一退出就全没了。
        // 取消导致的放回同样要落盘——事件已经回到队列，磁盘就该跟队列一致。
        persist()
    }

    /// 任务取消（-999 / CancellationError）不是服务端或网络故障，不应触发退避。
    private static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let urlError = error as? URLError, urlError.code == .cancelled { return true }
        return false
    }

    /// 在线时指数退避重试；离线时等 NWPathMonitor 恢复后再 flush。
    private func scheduleRetryIfNeeded(config: AnalyticsConfig) {
        guard isOnline else { return }
        scheduleFlush(delay: retryDelayNanos(config: config))
    }

    /// 5s → 10s → 20s → 40s → 60s（封顶）。
    private func retryDelayNanos(config: AnalyticsConfig) -> UInt64 {
        let base = max(config.flushDelayNanos, 5_000_000_000)
        let shift = min(max(consecutiveFailures - 1, 0), 4)
        let delay = base << shift
        return min(delay, 60_000_000_000)
    }

    // MARK: - 待发队列持久化
    //
    // 队列只存在内存里，意味着「网络故障时段的事件必然丢失」——而那恰恰是最该留痕的时段。
    // 2026-08-03 排查一笔 IAP 漏单时吃过这个亏：用户在离线状态下走完了整个购买流程，
    // paywall_view / purchase_start 全堆在内存队列里，App 一退出就蒸发，
    // 事后完全无法还原用户路径，只能靠 Apple 的 S2S 通知反推。
    // 这里把待发队列落盘，下次启动读回来继续补发；服务端以客户端上报的 ts 入库
    // （另用 created_at 记接收时间），所以补发不会打乱事件时序。

    private var pendingFileURL: URL? {
        guard let config = AnalyticsRuntime.config else { return nil }
        let fm = FileManager.default
        guard let base = try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                     appropriateFor: nil, create: true) else { return nil }
        let dir = base.appendingPathComponent("AnalyticsKit", isDirectory: true)
        if !fm.fileExists(atPath: dir.path) {
            guard (try? fm.createDirectory(at: dir, withIntermediateDirectories: true)) != nil else {
                return nil
            }
        }
        return dir.appendingPathComponent("\(config.appName)-pending.json")
    }

    /// 首次使用时把上次残留的待发事件读回队列（排在新事件之前，保持时序）。
    private func ensureLoaded() {
        guard !didLoadPersisted else { return }
        didLoadPersisted = true
        guard let url = pendingFileURL,
              let data = try? Data(contentsOf: url),
              let saved = try? JSONDecoder().decode([PendingEvent].self, from: data),
              !saved.isEmpty else { return }
        let config = AnalyticsRuntime.requireConfig()
        buffer = Array((saved + buffer).suffix(config.maxBufferSize))
        log.info("Analytics restored \(saved.count, privacy: .public) pending events from disk")
        scheduleFlush(delay: config.flushDelayNanos)
    }

    /// 原子写入当前待发队列；队列空则删文件，不留垃圾。
    ///
    /// 「取出一批准备发送」的那一刻故意不写盘：若进程恰在请求途中被杀，
    /// 下次启动会重发这批事件——宁可少量重复，也不接受丢失。
    private func persist() {
        guard let url = pendingFileURL else { return }
        guard !buffer.isEmpty else {
            try? FileManager.default.removeItem(at: url)
            return
        }
        guard let data = try? encoder.encode(buffer) else { return }
        do {
            try data.write(to: url, options: .atomic)
            var mutableURL = url
            var values = URLResourceValues()
            values.isExcludedFromBackup = true   // 埋点队列没有备份价值
            try? mutableURL.setResourceValues(values)
        } catch {
            log.debug("Analytics persist failed: \(String(describing: error), privacy: .public)")
        }
    }

    // MARK: - Internals

    private func propsWithinLimit(_ props: [String: AnalyticsValue], maxBytes: Int) -> Bool {
        guard !props.isEmpty else { return true }
        guard let data = try? encoder.encode(props) else { return false }
        return data.count <= maxBytes
    }

    /// 防抖调度：track() 每来一个事件就调一次，新的调度会取消上一个"待触发的定时器"。
    ///
    /// 注意 flushTask 只承载 sleep，**不承载 flush 本身**。早先的写法是
    /// `await self?.flush()` 直接跑在 flushTask 内，于是上一次 flush 已经越过 sleep、
    /// 正卡在 `URLSession.data(for:)` 上时，新事件触发的 `flushTask?.cancel()` 会把
    /// 在飞的 HTTP 请求一起掐断，URLSession 抛 -999 cancelled——埋点在自己取消自己。
    /// 事件虽然会被 requeue，但 requeue 把这次"自作自受"记成真实失败，退避一路涨到 60s。
    ///
    /// 这里把发送放进一个独立的非结构化 Task，脱离防抖的取消链；并发由 flush() 内的
    /// isFlushing 守。
    private func scheduleFlush(delay: UInt64) {
        flushTask?.cancel()
        flushTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay)
            if Task.isCancelled { return }
            Task { await self?.flush() }
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
