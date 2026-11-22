/** What the sidebar and footer say about orders. The wording follows the server's real capability (the deployment flag read from /dhan-live/status), never a hardcoded "no live orders". */
export type LiveCapability = { deployment_enabled: boolean; armed?: boolean } | null;

export const RESEARCH_ONLY = 'Read-only · decision support';
export const LIVE_POSSIBLE = 'Research is decision support only. Live orders run only from Signals > Forward Test for allowlisted strategies.';

export function sidebarWording(capability: LiveCapability): string {
  return capability?.deployment_enabled ? LIVE_POSSIBLE : RESEARCH_ONLY;
}

export function footerWording(capability: LiveCapability): string {
  if (!capability) return 'Research only';
  return capability.deployment_enabled ? `Live orders: Signals > Forward Test only${capability.armed ? ' (armed)' : ' (not armed)'}` : 'No live orders';
}
