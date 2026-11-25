import { useEffect, useId, useState, type FormEvent } from 'react';

import { ApiError, client } from '@/api/client';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTranslation } from '@/i18n/useI18n';

interface LoginView {
	id: string;
	revision: number;
	step: {
		title: string;
		title_key?: string | null;
		description: string;
		submit_label: string;
		submit_label_key?: string | null;
		fields: Array<{
			name: string;
			label: string;
			label_key?: string | null;
			kind: 'text' | 'secret';
			required: boolean;
			options?: Array<{ value: string; label: string }> | null;
		}>;
	};
}

export function ToolLoginDialog({
	agentId,
	sessionId,
	change,
}: {
	agentId: string;
	sessionId: string;
	change: number;
}) {
	const [view, setView] = useState<LoginView | null>(null);
	const [reload, setReload] = useState(0);
	const path = '/sessions/' + encodeURIComponent(sessionId) + '/tool-login';

	useEffect(() => {
		const controller = new AbortController();
		void client
			.get<LoginView | null>(
				path,
				{ agent_id: agentId },
				{
					silent: true,
					signal: controller.signal,
				},
			)
			.then((pending) => {
				if (!controller.signal.aborted) setView(pending);
			})
			.catch(() => {});
		return () => controller.abort();
	}, [agentId, change, path, reload]);

	if (!view) return null;
	return (
		<LoginForm
			key={view.id + ':' + view.revision}
			view={view}
			path={path + '/' + view.id}
			agentId={agentId}
			refresh={() => setReload((count) => count + 1)}
		/>
	);
}

function LoginForm({
	view,
	path,
	agentId,
	refresh,
}: {
	view: LoginView;
	path: string;
	agentId: string;
	refresh: () => void;
}) {
	const { t } = useTranslation();
	const formId = useId();
	const [pending, setPending] = useState(false);
	const [error, setError] = useState('');
	const label = (key: string | null | undefined, fallback: string) =>
		key ? t('chat.toolLogin.enterprise.' + key, { defaultValue: fallback }) : fallback;

	async function perform(method: 'POST' | 'DELETE', body?: unknown) {
		if (pending) return;
		setPending(true);
		setError('');
		try {
			await client.stream(path, {
				method,
				body,
				params: { agent_id: agentId },
				silent: true,
				timeoutMs: 45_000,
			});
			refresh();
		} catch (err) {
			if (err instanceof ApiError && (err.status === 404 || err.status === 409)) refresh();
			if (err instanceof ApiError) {
				const data = err.data;
				if (data && typeof data === 'object' && 'code' in data && typeof data.code === 'string') {
					const params = 'params' in data ? data.params as Record<string, string> : {};
					setError(t('chat.toolLogin.enterprise.' + data.code, {
						field: params?.field,
						route: params?.route,
						defaultValue: err.detail,
					}));
				} else {
					setError(err.detail);
				}
			} else {
				setError(t('chat.toolLogin.submitError'));
			}
		} finally {
			setPending(false);
		}
	}

	function submit(event: FormEvent<HTMLFormElement>) {
		event.preventDefault();
		const form = event.currentTarget;
		const values = Object.fromEntries(new FormData(form));
		form.reset();
		void perform('POST', { revision: view.revision, values });
	}

	return (
		<Dialog open>
			<DialogContent
				showCloseButton={false}
				onEscapeKeyDown={(event) => event.preventDefault()}
				onInteractOutside={(event) => event.preventDefault()}
			>
				<DialogHeader>
					<DialogTitle>{label(view.step.title_key, view.step.title)}</DialogTitle>
					<DialogDescription>{view.step.description}</DialogDescription>
				</DialogHeader>
				<form onSubmit={submit} autoComplete="off" className="grid gap-4">
					{view.step.fields.map((field, index) => {
						const id = formId + '-' + index;
						return (
							<div key={field.name} className="grid gap-2">
								<Label htmlFor={id}>{label(field.label_key, field.label)}</Label>
								{field.options ? (
									<select
										id={id}
										name={field.name}
										required={field.required}
										disabled={pending}
										defaultValue=""
										className="border-input bg-background h-9 rounded-md border px-3"
									>
										<option value="" />
										{field.options.map((option) => (
											<option key={option.value} value={option.value}>
												{option.label}
											</option>
										))}
									</select>
								) : (
									<Input
										id={id}
										name={field.name}
										type={field.kind === 'secret' ? 'password' : 'text'}
										autoComplete="off"
										required={field.required}
										disabled={pending}
										maxLength={4096}
									/>
								)}
							</div>
						);
					})}
					{error && (
						<p role="alert" className="text-destructive">
							{error}
						</p>
					)}
					<DialogFooter>
						<Button
							type="button"
							variant="outline"
							disabled={pending}
							onClick={() => void perform('DELETE')}
						>
							{t('chat.toolLogin.cancel')}
						</Button>
						<Button type="submit" disabled={pending}>
							{pending ? t('chat.toolLogin.working') : label(view.step.submit_label_key, view.step.submit_label)}
						</Button>
					</DialogFooter>
				</form>
			</DialogContent>
		</Dialog>
	);
}
