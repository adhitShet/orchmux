import Foundation

struct Question: Identifiable {
    let id: String
    let message: String
    let session: String
    let askedAt: String
    var answered: Bool
}

struct TaskDetail: Identifiable {
    let id: String
    let task: String
    let domain: String
    let session: String
    let status: String
    let result: String?
}

struct CompletedTask: Identifiable {
    let id: String
    let session: String
    let result: String
    let success: Bool
}
