import { client, getBaseUrl, getUserId } from './client';

export interface EnterpriseSkill {
	name: string;
	display_name: string;
	business: string;
	action: 'query' | 'save' | 'readback';
	description: string;
}

export interface EnterpriseAction {
	action_id: string;
	visible?: boolean;
	title?: string | null;
	status?: 'pending' | 'registered' | null;
	completed?: number | null;
	total?: number | null;
	can_resume?: boolean;
}

interface EnterpriseActionPage {
	actions: EnterpriseAction[];
	next_offset: number | null;
}

export const enterpriseSkillKey = (agentId: string | null) =>
	['enterprise-skills', getBaseUrl(), getUserId(), agentId] as const;

export const enterpriseActionKey = (agentId: string | null) =>
	['enterprise-actions', getBaseUrl(), getUserId(), agentId] as const;

const enterprisePath = (agentId: string) =>
	'/agent/' + encodeURIComponent(agentId) + '/enterprise-business';

export function listEnterpriseSkills(agentId: string, sessionId: string | null, signal: AbortSignal) {
	return client.get<EnterpriseSkill[]>(
		enterprisePath(agentId) + '/skills',
		sessionId ? { session_id: sessionId } : undefined,
		{ signal, silent: true },
	);
}

export function listEnterpriseActions(
	agentId: string,
	sessionId: string | null,
	offset: number,
	signal: AbortSignal,
) {
	return client.get<EnterpriseActionPage>(
		enterprisePath(agentId) + '/actions',
		{ offset: String(offset), ...(sessionId ? { session_id: sessionId } : {}) },
		{ signal, silent: true },
	);
}

export function resumeEnterpriseAction(agentId: string, sessionId: string, actionId: string) {
	return client.post<EnterpriseAction>(
		enterprisePath(agentId) + '/actions/' + encodeURIComponent(actionId) + '/resume',
		undefined,
		{ session_id: sessionId },
		{ silent: true },
	);
}
