import Foundation

/// Mirrors the web dashboard's `stripAnsi` + `cleanTermOutput` pipeline in
/// `/clean/shared.jsx`, ported to Swift.
///
/// The server's `/pane/:session` endpoint returns text that's already had most
/// ANSI escapes stripped, but the raw tmux capture still contains:
///   - terminal UI box-drawing rulers (`────────`)
///   - empty prompts (`❯`, `$`, `#`, `>`)
///   - stray terminal mode codes (`[?25h`)
///   - multiple blank lines
/// All of which look like junk in a non-terminal text view.
enum PaneProcessor {

    /// Run the full pipeline: ANSI strip (defensive) → terminal-noise filter.
    static func clean(_ input: String) -> String {
        cleanTermOutput(stripAnsi(input))
    }

    /// Remove ANSI CSI / OSC escape sequences and carriage returns.
    /// Defensive — the server's `/pane` already strips most of these.
    static func stripAnsi(_ s: String) -> String {
        var t = s
        // CSI sequences: ESC [ ... letter
        t = t.replacingOccurrences(of: #"\u{001B}\[[0-9;?]*[a-zA-Z~]"#,
                                   with: "",
                                   options: .regularExpression)
        // OSC sequences: ESC ] ... BEL or ESC \
        t = t.replacingOccurrences(of: #"\u{001B}\][^\u{0007}\u{001B}]*(\u{0007}|\u{001B}\\)"#,
                                   with: "",
                                   options: .regularExpression)
        // Stray single-char escapes
        t = t.replacingOccurrences(of: #"\u{001B}[^\[\]]"#,
                                   with: "",
                                   options: .regularExpression)
        // Carriage returns (we only want \n)
        t = t.replacingOccurrences(of: "\r", with: "")
        return t
    }

    /// Drop terminal-UI noise line-by-line, then collapse consecutive blanks
    /// and trim trailing empties.
    static func cleanTermOutput(_ s: String) -> String {
        var out: [String] = []
        var blanks = 0
        for line in s.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // Pure box-drawing / whitespace lines (U+2500..U+257F + spaces)
            if !trimmed.isEmpty && trimmed.unicodeScalars.allSatisfy(isBoxDrawingOrSpace) {
                continue
            }

            // Long horizontal rulers: 4+ runs of `-─━=╌`
            if rulerRegex.firstMatch(in: trimmed, range: nsRange(trimmed)) != nil {
                continue
            }

            // Terminal mode codes leftover, e.g. "[?25h"
            if modeCodeRegex.firstMatch(in: trimmed, range: nsRange(trimmed)) != nil {
                continue
            }

            // Stray ESC at start of line
            if trimmed.first == "\u{001B}" { continue }

            // Empty prompts: `❯`, `>`, `$`, `#` alone
            if emptyPromptRegex.firstMatch(in: trimmed, range: nsRange(trimmed)) != nil {
                continue
            }

            if trimmed.isEmpty {
                blanks += 1
                if blanks <= 1 { out.append("") }
                continue
            }

            blanks = 0
            out.append(line)
        }

        // Trim trailing blanks
        while let last = out.last, last.trimmingCharacters(in: .whitespaces).isEmpty {
            out.removeLast()
        }

        return out.joined(separator: "\n")
    }

    // MARK: - helpers

    private static func isBoxDrawingOrSpace(_ u: Unicode.Scalar) -> Bool {
        // Box Drawing block: U+2500..U+257F
        (u.value >= 0x2500 && u.value <= 0x257F) || u == " " || u == "\t"
    }

    private static let rulerRegex =
        try! NSRegularExpression(pattern: #"^[-─━=╌]{4,}$"#)
    private static let modeCodeRegex =
        try! NSRegularExpression(pattern: #"^\[\?[0-9]+[hl]"#)
    private static let emptyPromptRegex =
        try! NSRegularExpression(pattern: #"^[❯>$#]\s*$"#)

    private static func nsRange(_ s: String) -> NSRange {
        NSRange(s.startIndex..., in: s)
    }
}
