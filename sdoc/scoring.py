"""
scoring.py — score a submission against a ground_truth.json you have locally.
Approximates the formula in the participant README
(50% end-to-end + 30% Stage-1 macro-F1 + 20% Stage-3 defect-F1, with
NEEDS_REVIEW handling reported separately). The organizers' server
(POST /submit, i.e. `python -m sdoc run --submit`) is the real scorer.

  python -m sdoc score                       # output/submission.json vs data ground truth
  python -m sdoc score some.json --gt gt.json --show 20
"""

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]


def _f1(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return (2 * p * r / (p + r) if p + r else 0.0), p, r


def score(sub, gt):
    # Stage 1: category macro-F1
    per_cat = {}
    for c in CATEGORIES:
        tp = sum(1 for k in gt if gt[k]["category"] == c and sub.get(k, {}).get("category") == c)
        fp = sum(1 for k in gt if gt[k]["category"] != c and sub.get(k, {}).get("category") == c)
        fn = sum(1 for k in gt if gt[k]["category"] == c and sub.get(k, {}).get("category") != c)
        per_cat[c] = _f1(tp, fp, fn)[0]
    stage1 = sum(per_cat.values()) / len(CATEGORIES)

    # Stage 3: field-level defect F1
    tp = fp = fn = 0
    for k, g in gt.items():
        pred = set(sub.get(k, {}).get("defect_fields") or [])
        true = set(g["defect_fields"])
        tp += len(pred & true); fp += len(pred - true); fn += len(true - pred)
    stage3, s3p, s3r = _f1(tp, fp, fn)

    # End-to-end: a defective email counts as caught only if it was classified
    # BL_COMPARISON AND the exact defect fields were reported.
    e2e_tp = e2e_fp = e2e_fn = 0
    for k, g in gt.items():
        s = sub.get(k, {})
        pred_def = bool(s.get("has_defect"))
        exact = (s.get("category") == "BL_COMPARISON"
                 and set(s.get("defect_fields") or []) == set(g["defect_fields"]))
        if g["has_defect"] and pred_def and exact:
            e2e_tp += 1
        else:
            if pred_def:
                e2e_fp += 1
            if g["has_defect"]:
                e2e_fn += 1
    e2e = _f1(e2e_tp, e2e_fp, e2e_fn)[0]

    # Reliability: NEEDS_REVIEW with the right reason, and false escalations
    gt_review = [k for k, g in gt.items() if g["status"] == "NEEDS_REVIEW"]
    review_right = sum(1 for k in gt_review
                       if sub.get(k, {}).get("status") == "NEEDS_REVIEW"
                       and sub.get(k, {}).get("review_reason") == gt[k]["review_reason"])
    false_review = sum(1 for k, g in gt.items()
                       if g["status"] != "NEEDS_REVIEW" and sub.get(k, {}).get("status") == "NEEDS_REVIEW")
    false_alarm = sum(1 for k, g in gt.items() if not g["has_defect"] and sub.get(k, {}).get("has_defect"))

    exact_rows = sum(1 for k, g in gt.items()
                     if sub.get(k, {}).get("category") == g["category"]
                     and sub.get(k, {}).get("status") == g["status"]
                     and sub.get(k, {}).get("review_reason") == g["review_reason"]
                     and set(sub.get(k, {}).get("defect_fields") or []) == set(g["defect_fields"]))

    return {
        "final_score_approx": round(0.5 * e2e + 0.3 * stage1 + 0.2 * stage3, 4),
        "end_to_end_f1": round(e2e, 4),
        "stage1_macro_f1": round(stage1, 4),
        "stage3_defect_f1": round(stage3, 4),
        "stage3_precision": round(s3p, 4),
        "stage3_recall": round(s3r, 4),
        "per_category_f1": {c: round(v, 4) for c, v in per_cat.items()},
        "needs_review_correct": f"{review_right}/{len(gt_review)}",
        "false_needs_review": false_review,
        "false_alarms": false_alarm,
        "rows_fully_correct": f"{exact_rows}/{len(gt)}",
    }


def wrong_rows(sub, gt):
    """[(email_id, got, expected)] for every row that isn't fully correct."""
    rows = []
    for k, g in gt.items():
        s = sub.get(k, {})
        got = (s.get("category"), s.get("status"), s.get("review_reason"), sorted(s.get("defect_fields") or []))
        exp = (g["category"], g["status"], g["review_reason"], sorted(g["defect_fields"]))
        if got != exp:
            rows.append((k, got, exp))
    return rows
