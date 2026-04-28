import Foundation

struct DomainStatus: Identifiable {
    let id: String          // domain name
    let workers: [Worker]
    let queueDepth: Int
    let model: String
}

struct Worker: Identifiable {
    let id: String          // session name
    let session: String
    let domain: String
    let status: WorkerStatus
    let currentTask: String?
    let elapsedSeconds: Int?
    let paneProgress: String?
    let workerType: String

    enum WorkerStatus: String {
        case idle, busy, waiting, blocked, missing, offline, unknown

        var label: String {
            switch self {
            case .idle:    return "idle"
            case .busy:    return "running"
            case .waiting: return "asking"
            case .blocked: return "blocked"
            case .missing: return "gone"
            case .offline: return "offline"
            case .unknown: return "unknown"
            }
        }
    }

    var elapsedFormatted: String? {
        guard let s = elapsedSeconds else { return nil }
        if s < 60  { return "\(s)s" }
        if s < 3600 { return "\(s/60)m \(s%60)s" }
        return "\(s/3600)h \((s%3600)/60)m"
    }
}
