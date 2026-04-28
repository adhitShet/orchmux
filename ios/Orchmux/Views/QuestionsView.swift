import SwiftUI

struct QuestionsView: View {
    @EnvironmentObject var api: APIClient
    @State private var answeringQuestion: Question?
    @State private var answerText = ""

    var body: some View {
        NavigationStack {
            Group {
                if api.questions.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "checkmark.bubble")
                            .font(.system(size: 48))
                            .foregroundStyle(.secondary)
                        Text("No pending questions")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    List {
                        ForEach(api.questions) { q in
                            QuestionRowView(question: q) {
                                answeringQuestion = q
                                answerText = ""
                            } onDismiss: {
                                Task { await api.dismissQuestion(id: q.id) }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Questions")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(action: { api.refresh() }) {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .sheet(item: $answeringQuestion) { q in
                AnswerSheet(question: q, isPresented: Binding(
                    get: { answeringQuestion != nil },
                    set: { if !$0 { answeringQuestion = nil } }
                ))
            }
        }
    }
}

struct QuestionRowView: View {
    let question: Question
    let onAnswer: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                if !question.session.isEmpty {
                    Text(question.session)
                        .font(.caption)
                        .fontWeight(.semibold)
                        .foregroundStyle(.blue)
                }
                Spacer()
                if !question.askedAt.isEmpty {
                    Text(question.askedAt)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

            Text(question.message)
                .font(.body)
                .textSelection(.enabled)

            HStack(spacing: 12) {
                Button(action: onAnswer) {
                    Label("Answer", systemImage: "arrowshape.turn.up.left.fill")
                        .font(.caption)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)

                Button(role: .destructive, action: onDismiss) {
                    Label("Dismiss", systemImage: "xmark")
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .padding(.vertical, 4)
    }
}

struct AnswerSheet: View {
    let question: Question
    @Binding var isPresented: Bool
    @State private var answerText = ""
    @EnvironmentObject var api: APIClient

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                GroupBox("Question") {
                    Text(question.message)
                        .font(.body)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                Text("Your Answer")
                    .font(.headline)

                TextEditor(text: $answerText)
                    .frame(minHeight: 100)
                    .padding(8)
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 10))

                Spacer()
            }
            .padding()
            .navigationTitle("Answer")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { isPresented = false }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") {
                        Task {
                            try? await api.answerQuestion(id: question.id, answer: answerText)
                            isPresented = false
                        }
                    }
                    .disabled(answerText.trimmingCharacters(in: .whitespaces).isEmpty)
                    .fontWeight(.semibold)
                }
            }
        }
    }
}
