"""Five XFL 2 sample trades for the owner's spot check, each exercising a path:
1. Aging-production swap that closes BOTH sides' critical needs
2. Consolidation 2-for-1 into the league's best WR (package premium)
3. Rebuild sells its production-priced stud - return deliberately mixed
4. Control: a rebuilder renting a deep-decline RB (should get flagged hard)
5. Rental at a discount: press team sells Barkley to a wait team for a pick
"""
from analysis import trade_eval
from analysis.trade_targets.board import build_board

LID = "1315386978904084480"
board = build_board(LID)

TRADES = [
    ("1. JT <-> Lamb (both close criticals)",
     "kierankieran", ["jonathan taylor"], "dezdroppedit27", ["ceedee"]),
    ("2. Consolidation: Tet + Tate -> JSN",
     "bigbuttboi", ["tetairoa", "carnell"], "shivvv", ["jaxon"]),
    ("3. Spugz sells Jefferson for 1st + Smith",
     "spugz13", ["jefferson"], "kbmckenna", ["2027 1st", "devonta"]),
    ("4. CONTROL: jq rents Henry for a 1st",
     "jqsimonds22", ["2027 1st"], "shivvv", ["henry"]),
    ("5. Barkley rental to Vicdank for a 2027 2nd",
     "kierankieran", ["barkley"], "Vicdank", ["2027 2nd"]),
]

for title, oa, sa, ob, sb in TRADES:
    print("=" * 78)
    print(title)
    out = trade_eval.evaluate_from_board(board, oa, sa, ob, sb)
    if not out["ok"]:
        print("  PROBLEM:", out["problem"])
        continue
    bp = out["best_piece"]
    print(f"  best piece: {bp['name']} ({bp['value']:,}) -> {bp['to']}")
    for s in out["sides"]:
        pieces = ", ".join(f"{p['name']}" for p in s["sends"])
        print(f"  {s['owner']} ({s['window']}) sends: {pieces}")
        for r in s["read"]:
            print(f"     - {r}")
    print()
