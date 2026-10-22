import { Activity, CircleAlert, ShieldCheck } from 'lucide-react';
import type { WorkspaceState } from '../components/useWorkspace';

export function AuthPage({ workspace }: { workspace: Pick<WorkspaceState, 'error' | 'registering' | 'setError' | 'setRegistering' | 'submitAuth' | 'verificationStatus' | 'verificationToken'> }) {
  const {
  error,
  registering,
  setError,
  setRegistering,
  submitAuth,
  verificationStatus,
  verificationToken
} = workspace;

  if (verificationToken) {
    const message = verificationStatus === 'verifying'
      ? 'Verifying your email…'
      : verificationStatus === 'verified'
        ? 'Email verified. Sign in, then set up app-based two-factor authentication to access brokers.'
        : verificationStatus === 'expired'
          ? 'This verification link has expired. Sign in and request a new verification email.'
          : verificationStatus === 'reused'
            ? 'This verification link was already used. Sign in to continue.'
            : verificationStatus === 'invalid'
              ? 'This verification link is malformed or invalid. Request a new verification email after signing in.'
              : 'Email verification could not be completed. Try again or request a new verification email.';
    return (
      <main className="auth-layout">
        <section className="auth-panel" aria-live="polite">
          <div className="brand-lockup"><span className="brand-symbol"><Activity size={20} /></span><span>Trading Copilot</span></div>
          <h1>Email verification</h1>
          <p>{message}</p>
          {verificationStatus !== 'verifying' && (
            <button className="primary-button" type="button" onClick={() => window.location.assign('/')}>Continue</button>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="auth-layout">
      <section className="auth-panel">
        <div className="brand-lockup"><span className="brand-symbol"><Activity size={20} /></span><span>Trading Copilot</span></div>
        <p className="eyebrow">PERSONAL INVESTMENT WORKSPACE</p>
        <h1>{registering ? 'Create your account' : 'Sign in'}</h1>
        {registering && <p>We’ll email a verification link. Verify your address, then sign in to connect a broker.</p>}
        <form className="form-stack" onSubmit={submitAuth}>
          <label>Email address<input name="email" type="email" autoComplete="email" required /></label>
          <label>Password<input name="password" type="password" minLength={12} autoComplete={registering ? 'new-password' : 'current-password'} required /></label>
          <button className="primary-button" type="submit">{registering ? 'Create account' : 'Sign in'}</button>
        </form>
        {error && <p className="error-message" role="alert"><CircleAlert size={16} />{error}</p>}
        <button className="text-button" type="button" onClick={() => { setError(''); setRegistering((value) => !value); }}>
          {registering ? 'Already have an account? Sign in' : 'Create an account'}
        </button>
        <p className="security-note"><ShieldCheck size={15} /> Secure server-side sessions · no broker connection</p>
      </section>
    </main>
  );
}
