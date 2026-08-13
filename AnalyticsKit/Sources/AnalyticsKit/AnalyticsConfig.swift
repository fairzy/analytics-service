import Foundation

/// 各 App 接入时差异配置。启动时调用一次 `AnalyticsClient.install(_:)`。
public struct AnalyticsConfig: Sendable {
    /// 白名单 app 名：liveai / dinopedia / animal-friends / earthtrip
    public var appName: String
    /// 默认生产埋点端点
    public var trackURL: URL
    /// AES-256 key（64 hex）。`nil` 则明文 JSON（兼容旧客户端）。
    public var payloadKeyHex: String?
    /// Keychain service，按 App 区分，如 `ai.talent.liveai.analytics`
    public var keychainService: String
    /// Keychain account，默认 `analyticsClientId`
    public var keychainAccount: String
    /// 旧版 UserDefaults installId key；有则首次启动迁移到 Keychain
    public var legacyInstallIdDefaultsKey: String?
    public var flushDelayNanos: UInt64
    public var maxBatchSize: Int
    public var maxBufferSize: Int
    public var maxPropsBytes: Int
    /// 展示用版本号；默认 Bundle short+build
    public var appVersionProvider: @Sendable () -> String?
    /// 机型标识；默认 utsname machine
    public var deviceModelProvider: @Sendable () -> String

    public init(
        appName: String,
        trackURL: URL = URL(string: "https://analytics.picturebookpedia.cn/api/events/track")!,
        payloadKeyHex: String? = nil,
        keychainService: String,
        keychainAccount: String = "analyticsClientId",
        legacyInstallIdDefaultsKey: String? = nil,
        flushDelayNanos: UInt64 = 5_000_000_000,
        maxBatchSize: Int = 100,
        maxBufferSize: Int = 200,
        maxPropsBytes: Int = 2_048,
        appVersionProvider: (@Sendable () -> String?)? = nil,
        deviceModelProvider: (@Sendable () -> String)? = nil
    ) {
        self.appName = appName
        self.trackURL = trackURL
        self.payloadKeyHex = payloadKeyHex
        self.keychainService = keychainService
        self.keychainAccount = keychainAccount
        self.legacyInstallIdDefaultsKey = legacyInstallIdDefaultsKey
        self.flushDelayNanos = flushDelayNanos
        self.maxBatchSize = maxBatchSize
        self.maxBufferSize = maxBufferSize
        self.maxPropsBytes = maxPropsBytes
        self.appVersionProvider = appVersionProvider ?? { AnalyticsDefaults.appVersion() }
        self.deviceModelProvider = deviceModelProvider ?? { AnalyticsDefaults.deviceModel() }
    }
}

enum AnalyticsRuntime {
    /// 启动期 install 一次；Keychain 静态方法与 actor 共用。
    nonisolated(unsafe) static var config: AnalyticsConfig?

    static func requireConfig() -> AnalyticsConfig {
        guard let config else {
            preconditionFailure("AnalyticsClient.install(_:) must be called before use (typically App launch / AuthManager.init).")
        }
        return config
    }
}

enum AnalyticsDefaults {
    static func appVersion() -> String? {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        return "\(v) (\(b))"
    }

    static func deviceModel() -> String {
        var system = utsname()
        uname(&system)
        return withUnsafePointer(to: &system.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) {
                String(validatingCString: $0) ?? "unknown"
            }
        }
    }
}
