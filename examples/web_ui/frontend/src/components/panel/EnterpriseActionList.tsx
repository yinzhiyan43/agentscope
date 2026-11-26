import { LoaderCircle } from 'lucide-react';

import type { EnterpriseAction } from '@/api/enterpriseBusiness';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Item, ItemContent } from '@/components/ui/item';
import { Progress } from '@/components/ui/progress';
import { useTranslation } from '@/i18n/useI18n';

interface EnterpriseActionListProps {
	actions: EnterpriseAction[];
	resumeDisabled: boolean;
	resumingId?: string;
	onResume: (actionId: string) => void;
}

export function EnterpriseActionList({
	actions,
	resumeDisabled,
	resumingId,
	onResume,
}: EnterpriseActionListProps) {
	const { t } = useTranslation();

	return (
		<ul className="flex flex-col gap-2" aria-label={t('panel.skill.businessActions.title')}>
			{actions.map(({ action_id, title, status, completed, total, can_resume }) => {
				const progress = completed != null && total != null && total > 0
					? {
						text: t('panel.skill.businessActions.progress', { completed, total }),
						value: Math.min(100, Math.max(0, (completed / total) * 100)),
					}
					: null;
				const resuming = resumingId === action_id;

				return (
					<Item key={action_id} asChild variant="outline" className="items-end">
						<li>
							<ItemContent className="min-w-0 basis-44">
								<span className="font-medium wrap-anywhere">{title}</span>
								{(status || progress) && (
									<div className="flex flex-wrap items-center gap-x-2 gap-y-1">
										{status && (
												<Badge variant={status === 'registered' ? 'outline' : 'secondary'}>
													{t('panel.skill.businessActions.' + status)}
												</Badge>
										)}
										{progress && (
												<span className="text-xs text-muted-foreground tabular-nums">{progress.text}</span>
												)}
									</div>
								)}
								{progress && (
									<Progress className="mt-1" value={progress.value} aria-label={progress.text} />
								)}
							</ItemContent>
							{status === 'pending' && can_resume !== false && (
								<Button
									type="button"
									variant="outline"
									size="sm"
									className="h-11 w-full sm:h-8 sm:w-auto"
									disabled={resumeDisabled}
									onClick={() => onResume(action_id)}
								>
									{resuming && <LoaderCircle className="motion-safe:animate-spin" aria-hidden="true" />}
									{t(resuming ? 'panel.skill.businessActions.resuming' : 'panel.skill.businessActions.resume')}
								</Button>
							)}
						</li>
					</Item>
				);
			})}
		</ul>
	);
}
