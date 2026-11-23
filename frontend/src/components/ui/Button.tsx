import type { ButtonHTMLAttributes, ReactNode } from 'react';

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; size?: 'sm' | 'md'; loading?: boolean; icon?: ReactNode };

/** One button. An icon-only button (no text children) must carry an aria-label. */
export function Button({ variant = 'secondary', size = 'md', loading = false, icon, children, disabled, type = 'button', ...rest }: Props) {
  if (!children && !rest['aria-label']) throw new Error('An icon-only Button needs an aria-label');
  return (
    <button {...rest} type={type} disabled={disabled || loading} aria-busy={loading || undefined} className={`ui-btn ui-btn-${variant} ui-btn-${size}${children ? '' : ' ui-btn-icon'} ${rest.className ?? ''}`.trim()}>
      {loading ? <span className="ui-spinner" aria-hidden="true" /> : icon}
      {children}
    </button>
  );
}
