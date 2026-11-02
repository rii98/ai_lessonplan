import { clsx } from "clsx";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import s from "./Feedback.module.css";

interface ToastItem { id: number; message: ReactNode; tone: "default" | "danger"; action?: { label: string; run: () => void } }
interface ToastApi {
  show(message: ReactNode, opts?: { tone?: "default" | "danger"; action?: ToastItem["action"]; ms?: number }): void;
  error(message: ReactNode): void;
}

const Ctx = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((x) => x.id !== id)), []);
  const show = useCallback<ToastApi["show"]>((message, { tone = "default", action, ms = 4200 } = {}) => {
    const id = ++seq.current;
    setItems((xs) => [...xs.slice(-2), { id, message, tone, action }]);
    setTimeout(() => dismiss(id), ms);
  }, [dismiss]);
  const api = useMemo<ToastApi>(() => ({ show, error: (m) => show(m, { tone: "danger", ms: 6000 }) }), [show]);
  return (
    <Ctx.Provider value={api}>
      {children}
      <div className={s.toasts} aria-live="polite" aria-atomic="false">
        {items.map((t) => (
          <div key={t.id} className={clsx(s.toast, t.tone === "danger" && s.toastDanger)} role={t.tone === "danger" ? "alert" : "status"}>
            <span>{t.message}</span>
            {t.action && <button type="button" onClick={() => { t.action!.run(); dismiss(t.id); }}>{t.action.label}</button>}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const c = useContext(Ctx);
  if (!c) throw new Error("useToast() needs <ToastProvider>");
  return c;
}
