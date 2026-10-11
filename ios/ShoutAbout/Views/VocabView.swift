import SwiftUI

/// The hub's custom vocabulary (`GET /vocab`), cached on the phone for offline mode, plus
/// "add a word" (`POST /vocab`). Terms live on the PC in vocab.txt; this is a remote control.
struct VocabView: View {
    @Environment(AppModel.self) private var model
    @State private var newTerm = ""
    @State private var busy = false
    @FocusState private var fieldFocused: Bool

    var body: some View {
        List {
            Section {
                HStack {
                    TextField("Add a word or name", text: $newTerm)
                        .focused($fieldFocused)
                        .submitLabel(.done)
                        .onSubmit(add)
                    Button("Add", action: add)
                        .font(Theme.Typography.bold(16))
                        .disabled(busy || newTerm.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                if let message = model.vocabMessage {
                    Text(message)
                        .font(Theme.Typography.body(13))
                        .foregroundStyle(.secondary)
                }
            }
            Section("On your PC (\(model.vocab.count))") {
                if model.vocab.isEmpty {
                    Text("No words yet. Pull to refresh.")
                        .foregroundStyle(.secondary)
                }
                ForEach(model.vocab, id: \.self) { term in
                    Text(term).font(Theme.Typography.body(16))
                }
            }
        }
        .navigationTitle("Custom words")
        .refreshable { await model.refreshVocab() }
        .task { await model.refreshVocab(silently: true) }
    }

    private func add() {
        let term = newTerm
        guard !term.trimmingCharacters(in: .whitespaces).isEmpty, !busy else { return }
        busy = true
        Task {
            await model.addVocab(term)
            busy = false
            newTerm = ""
        }
    }
}
