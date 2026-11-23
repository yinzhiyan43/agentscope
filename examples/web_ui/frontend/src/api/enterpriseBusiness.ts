import { client, getBaseUrl, getUserId } from './client';

export interface EnterpriseSkill {
	name: string;
	display_name: string;
	business: string;
	action: 'query' | 'save' | 'readback';
	description: string;
}

export const enterpriseSkillKey = (agentId: string | null) =>
	['enterprise-skills', getBaseUrl(), getUserId(), agentId] as const;

const enterprisePath = (agentId: string) =>
	'/agent/' + encodeURIComponent(agentId) + '/enterprise-business';

export function listEnterpriseSkills(agentId: string, sessionId: string | null, signal: AbortSignal) {
	return client.get<EnterpriseSkill[]>(
		enterprisePath(agentId) + '/skills',
		sessionId ? { session_id: sessionId } : undefined,
		{ signal, silent: true },
	);
}
