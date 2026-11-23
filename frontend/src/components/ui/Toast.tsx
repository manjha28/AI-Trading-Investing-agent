import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

type ToastItem = { id: number; text: string; tone: 'info' | 'positive' | 'negative' };
const Ctx = createContext<(text: string, tone?: ToastItem['tone']) => void>(() => undefined);
export const useToast = () => useContext(Ctx);

/** Transient messages in an aria-live region. Errors stay until dismissed; others close after 6 seconds. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const dismiss = useCallback((id: number) => setItems((cur) => cur.filter((t) => t.id !== id)), []);
  const push = useCallback((text: string, tone: ToastItem['tone'] = 'info') => {
    const id = Date.now() + Math.random();
    setItems((cur) => [...cur, { id, text, tone }]);
    if (tone !== 'negative') window.setTimeout(() => dismiss(id), 6000);
  }, [dismiss]);
  const value = useMemo(() => push, [push]);
  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="ui-toasts" aria-live="polite" aria-atomic="false">{items.map((t) => <div key={t.id} className={`ui-toast ui-toast-${t.tone}`} role={t.tone === 'negative' ? 'alert' : 'status'}>{t.text}<button type="button" aria-label="Dismiss" onClick={() => dismiss(t.id)}>×</button></div>)}</div>
    </Ctx.Provider>
  );
}
