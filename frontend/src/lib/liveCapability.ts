/** What the sidebar and footer say about orders. The wording follows the server's real capability (the deployment flag read from /dhan-live/status), never a hardcoded "no live orders". */
export type LiveCapability = { deployment_enabled: boolean; armed?: boolean } | null;

export const RESEARCH_ONLY = 'Read + order execution · orders off until armed';
export const LIVE_POSSIBLE = 'Read + order execution · live orders from Forward Test (Dhan) and Crypto (Delta) when armed';

export function sidebarWording(capability: LiveCapability): string {
  return capability?.deployment_enabled ? LIVE_POSSIBLE : RESEARCH_ONLY;
}

export function footerWording(capability: LiveCapability): string {
  if (!capability) return 'Read + order execution (status loading)';
  return capability.deployment_enabled ? `Live orders: Forward Test (Dhan), Crypto (Delta)${capability.armed ? ' (armed)' : ' (not armed)'}` : 'Orders off (switch off on this deployment)';
}
