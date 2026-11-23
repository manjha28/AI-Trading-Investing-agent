import { useRef, type ClipboardEvent, type KeyboardEvent } from 'react';

/** Six single-digit boxes: auto-advance, Backspace steps back, paste fills all six. Reports the digits as one string. */
export function CodeInput({ value, onChange, length = 6, label = 'Authenticator code' }: { value: string; onChange: (value: string) => void; length?: number; label?: string }) {
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const digits = value.replace(/\D/g, '').slice(0, length).padEnd(length, ' ').split('');
  const set = (index: number, digit: string) => {
    const next = digits.map((d, i) => (i === index ? digit : d)).join('').replace(/ /g, '');
    onChange(next);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>, i: number) => {
    if (e.key === 'Backspace' && digits[i] === ' ' && i > 0) { e.preventDefault(); set(i - 1, ' '); refs.current[i - 1]?.focus(); }
    else if (e.key === 'ArrowLeft' && i > 0) refs.current[i - 1]?.focus();
    else if (e.key === 'ArrowRight' && i < length - 1) refs.current[i + 1]?.focus();
  };
  const onPaste = (e: ClipboardEvent<HTMLInputElement>) => {
    const pasted = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, length);
    if (!pasted) return;
    e.preventDefault(); onChange(pasted); refs.current[Math.min(pasted.length, length - 1)]?.focus();
  };
  return (
    <div className="ui-code" role="group" aria-label={label}>
      {digits.map((d, i) => (
        <input key={i} ref={(el) => { refs.current[i] = el; }} inputMode="numeric" autoComplete={i === 0 ? 'one-time-code' : 'off'} maxLength={1} aria-label={`${label} digit ${i + 1}`} value={d === ' ' ? '' : d}
          onChange={(e) => { const c = e.target.value.replace(/\D/g, '').slice(-1); if (!c) return; set(i, c); if (i < length - 1) refs.current[i + 1]?.focus(); }}
          onKeyDown={(e) => onKey(e, i)} onPaste={onPaste} onFocus={(e) => e.target.select()} />
      ))}
    </div>
  );
}
