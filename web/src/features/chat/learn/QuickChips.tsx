import { Icon } from "@/ui";
import type { LearnAction, LearnState } from "../types";
import s from "./learn.module.css";

/**
 * One tap instead of typing (docs §5.6 W4): ask for a hint or an example, say "I know
 * this", change the pace, take a break — or pick what comes next at a branch.
 * Every chip is a deterministic action on the server (no guessing what was meant).
 */
export function QuickChips({ state, onAction }: { state: LearnState; onAction: (a: LearnAction, label: string, value?: string) => void }) {
  if (state.choices.length) {
    return (
      <div className={s.chips} aria-label="Choose what to learn next">
        {state.choices.map((id) => {
          const o = state.objectives.find((x) => x.id === id);
          return o && <button key={id} type="button" className={s.choice} onClick={() => onAction("choose", `👉 ${o.title}`, id)}><Icon.ArrowRight size={13} />{o.title}</button>;
        })}
      </div>
    );
  }
  const chips: [LearnAction, string, JSX.Element][] = [
    ["hint", "Hint", <Icon.Lightbulb key="h" size={13} />],
    ["example", "Show an example", <Icon.BookOpen key="e" size={13} />],
    ["skip", "I know this", <Icon.Check key="s" size={13} />],
    state.pace === "fast" ? ["slower", "Slow down", <Icon.ChevronDown key="p" size={13} />] : ["faster", "Go faster", <Icon.ChevronUp key="p" size={13} />],
  ];
  return (
    <div className={s.chips} aria-label="Quick replies">
      {chips.map(([a, l, icon]) => <button key={a} type="button" onClick={() => onAction(a, l)}>{icon}{l}</button>)}
    </div>
  );
}
