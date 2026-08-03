import Foundation

/// 事件 props 支持的值类型（与 analytics-service JSON 兼容）。
///
/// 可解码是为了让未发送成功的事件能落盘、并在下次启动读回来补发
/// （见 `AnalyticsClient` 的待发队列持久化）。
public enum AnalyticsValue: Codable, Sendable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        }
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        // Bool 必须先试：否则 true/false 可能被数字分支吞掉。
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else {
            self = .string(try container.decode(String.self))
        }
    }
}
