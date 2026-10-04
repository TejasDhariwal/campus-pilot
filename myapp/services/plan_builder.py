from datetime import date


def build_proposed_plan(action_items, deadlines, saved_items_by_action):
    """Pair notice actions with dates only when the extracted relationship is clear."""
    dates_by_document = {}
    eligible_date_ids = {item["date"].id for item in deadlines}
    for deadline in deadlines:
        dates_by_document.setdefault(deadline["document"].id, []).append(deadline)

    proposed_items = []
    for item in action_items:
        action = item["action"]
        source_deadlines = dates_by_document.get(item["document"].id, [])
        matching_deadline = next(
            (
                deadline
                for deadline in source_deadlines
                if action.due_date
                and deadline["date"].normalized_date == action.due_date
            ),
            None,
        )
        if matching_deadline is None and not action.due_date and len(source_deadlines) == 1:
            matching_deadline = source_deadlines[0]

        saved_plan_item = saved_items_by_action.get(action.id)
        if saved_plan_item and not saved_plan_item.is_active:
            continue

        default_date = action.due_date
        if default_date is None and matching_deadline:
            default_date = matching_deadline["date"].normalized_date
        # A saved blank date is an intentional edit, not a cue to restore the suggestion.
        planned_date = (
            saved_plan_item.planned_date
            if saved_plan_item
            else default_date
        )
        associated_date = (
            saved_plan_item.document_date
            if (
                saved_plan_item
                and saved_plan_item.document_date_id in eligible_date_ids
            )
            else matching_deadline["date"] if matching_deadline else None
        )
        proposed_items.append(
            {
                "action": action,
                "document": item["document"],
                "description": (
                    saved_plan_item.description
                    if saved_plan_item and saved_plan_item.description
                    else action.description
                ),
                "planned_date": planned_date,
                "plan_item": saved_plan_item,
                "document_date": associated_date,
                "is_suggestion": saved_plan_item is None,
            }
        )
    proposed_items.sort(
        key=lambda item: (
            item["planned_date"] is None,
            item["planned_date"] or date.max,
            item["document"].title,
            item["action"].position,
        )
    )
    return proposed_items
