"""Generate docs/architecture.svg: swimlanes showing where the AI, the code and the human each act."""

import pathlib

W = 880
LX = 74
GAP = 14
N_A = 7
BW = (W - LX - (N_A - 1) * GAP - 4) / N_A
ROW_H = 56
BOX_H = 42
FS = 10
LANES = [
    ("AI", "probabilistic", "#fff4e0", "#c77700"),
    ("CODE", "deterministic", "#e8f1fb", "#1f5fa8"),
    ("HUMAN", "audit gate", "#eaf6ea", "#2c7a2c"),
]
LANE_A = [
    (1, "A1 Ingest SEC\\ntext, hash"),
    (1, "A2 Select\\nclauses"),
    (0, "A3 LLM drafts\\nspec (untrusted)"),
    (1, "A4 Parse, locate\\nquotes, validate"),
    (1, "A5 Verify, seal,\\nsemantic checks"),
    (2, "A6 Gate 1: signed\\napproval"),
    (1, "A7 Registry\\n(attestation ok)"),
]
LANE_B = [
    (0, "B1 Extract facts\\n(untrusted)"),
    (1, "B2 Verify, seal,\\ntie-outs"),
    (1, "B3 Evaluate AST,\\nround, classify"),
    (2, "B4 Gate 2: review\\nbreach/thin/sample"),
    (1, "B5 Sealed,\\nhash-chained log"),
]


def build() -> str:
    out: list[str] = []
    set_h = 3 * ROW_H
    top_a, top_b = 22, 22 + set_h + 58
    height = top_b + set_h + 30
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" font-family="Helvetica,Arial,sans-serif">')
    for cid, color in (("a", "#333"), ("r", "#b00020"), ("g", "#777")):
        out.append(
            f'<defs><marker id="{cid}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
            f'<path d="M0,0 L8,4 L0,8 z" fill="{color}"/></marker></defs>'
        )
    pos: dict[tuple[str, int], tuple[float, float]] = {}

    def lane_set(tag: str, top: int, title: str, boxes: list[tuple[int, str]], anchor: str, tx: float) -> None:
        out.append(f'<text x="{tx}" y="{top - 8}" font-size="12.5" font-weight="bold" text-anchor="{anchor}">{title}</text>')
        for i, (name, sub, bg, fg) in enumerate(LANES):
            y = top + i * ROW_H
            out.append(f'<rect x="1" y="{y}" width="{W - 2}" height="{ROW_H - 2}" fill="{bg}" stroke="{fg}"/>')
            out.append(f'<text x="6" y="{y + ROW_H / 2 - 3}" font-size="11" font-weight="bold" fill="{fg}">{name}</text>')
            out.append(f'<text x="6" y="{y + ROW_H / 2 + 10}" font-size="9" fill="{fg}">{sub}</text>')
        for i, (lane, text) in enumerate(boxes):
            x = LX + i * (BW + GAP)
            y = top + lane * ROW_H + (ROW_H - 2 - BOX_H) / 2
            fg = LANES[lane][3]
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{BW:.1f}" height="{BOX_H}" rx="4" fill="white" stroke="{fg}" stroke-width="1.6"/>')
            for j, line in enumerate(text.split("\\n")):
                out.append(f'<text x="{x + BW / 2:.1f}" y="{y + 17 + j * 13:.1f}" font-size="{FS}" text-anchor="middle">{line}</text>')
            pos[(tag, i)] = (x, y)
        for i in range(len(boxes) - 1):
            x0, y0 = pos[(tag, i)]
            x1, y1 = pos[(tag, i + 1)]
            ax, ay = x0 + BW, y0 + BOX_H / 2
            bx, by = x1, y1 + BOX_H / 2
            mx = ax + GAP / 2
            out.append(f'<path d="M{ax:.1f},{ay:.1f} L{mx:.1f},{ay:.1f} L{mx:.1f},{by:.1f} L{bx:.1f},{by:.1f}" stroke="#333" stroke-width="1.4" fill="none" marker-end="url(#a)"/>')

    lane_set("a", top_a, "LANE A: compile the contract (once per agreement or amendment)", LANE_A, "start", 2)
    lane_set("b", top_b, "LANE B: quarterly test (every borrower, every period)", LANE_B, "end", W - 2)

    # retry loop: parse/validate failure goes back to the model, validation messages only, max 2 attempts
    x3, y3 = pos[("a", 2)]
    x4, y4 = pos[("a", 3)]
    gy = top_a + ROW_H - 1
    out.append(
        f'<path d="M{x4 + 28:.1f},{y4:.1f} L{x4 + 28:.1f},{gy} L{x3 + BW - 28:.1f},{gy} L{x3 + BW - 28:.1f},{y3 + BOX_H:.1f}" '
        'stroke="#b00020" stroke-width="1.3" stroke-dasharray="4,3" fill="none" marker-end="url(#r)"/>'
    )
    out.append(f'<text x="{x4 + BW + 6:.1f}" y="{y3 + 14:.1f}" font-size="10" fill="#b00020">fail: validation messages only,</text>')
    out.append(f'<text x="{x4 + BW + 6:.1f}" y="{y3 + 27:.1f}" font-size="10" fill="#b00020">max 2 attempts, then ESCALATED</text>')

    xa, ya = pos[("a", 6)]
    xb, yb = pos[("b", 0)]
    mid = top_a + set_h + 20
    out.append(
        f'<path d="M{xa + BW / 2:.1f},{ya + BOX_H:.1f} L{xa + BW / 2:.1f},{mid} L{xb + BW / 2:.1f},{mid} L{xb + BW / 2:.1f},{yb:.1f}" '
        'stroke="#777" stroke-width="1.3" stroke-dasharray="2,3" fill="none" marker-end="url(#g)"/>'
    )
    out.append(f'<text x="{xb + BW / 2 + 8:.1f}" y="{mid - 4}" font-size="10" fill="#555">approved, signed, hashed spec drives every quarterly test</text>')
    out.append(
        f'<text x="{W / 2}" y="{top_b + set_h + 17}" font-size="10.5" fill="#b00020" text-anchor="middle">'
        'STALE: an amendment, restatement or revoked spec is a ledger event that re-opens Lane A and re-queues dependent results</text>'
    )
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    (pathlib.Path(__file__).parent / "architecture.svg").write_text(build())
