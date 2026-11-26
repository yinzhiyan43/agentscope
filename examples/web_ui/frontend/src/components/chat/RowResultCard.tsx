import type { ContentBlock, ToolResultBlock } from '@agentscope-ai/agentscope/message';
import { ChevronRight, Reply } from 'lucide-react';
import { useState } from 'react';

import { getResultText } from './tool-renderers/_shared';
import type { CardQuote } from '@/api/chat';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useTranslation } from '@/i18n/useI18n';

export type SendCardReply = (content: ContentBlock[], quote: CardQuote) => Promise<void> | void;

function readCard(result: ToolResultBlock) {
	if (result.state !== 'success') return null;
	const text = getResultText(result);
	if (!text.includes('"row_card"')) return null;
	try {
		const { row_card: card, records, list_ref: ref } = JSON.parse(text);
		if (
			card?.version !== 1 ||
			typeof card.title !== 'string' ||
			!Array.isArray(card.columns) ||
			!card.columns.length ||
			!card.columns.every(
				(c: { key?: unknown; label?: unknown } | null) =>
					c && typeof c.key === 'string' && typeof c.label === 'string',
			) ||
			!Array.isArray(records) ||
			!records.every((row) => row && typeof row === 'object' && !Array.isArray(row))
		)
			return null;
		return {
			title: card.title as string,
			columns: card.columns as { key: string; label: string }[],
			records: records as Record<string, unknown>[],
			canOpen: typeof ref === 'string' && ref.length > 0 && /^query_recent_[a-zA-Z0-9_-]+$/.test(result.name),
		};
	} catch {
		return null;
	}
}

export function RowResultCard({
	messageId,
	result,
	onSend,
	disabled,
}: {
	messageId: string;
	result: ToolResultBlock;
	onSend?: SendCardReply;
	disabled: boolean;
}) {
	const { t } = useTranslation();
	const [replying, setReplying] = useState(false);
	const [draft, setDraft] = useState('');
	const [sending, setSending] = useState(false);
	const card = readCard(result);
	if (!card) return null;
	const busy = disabled || sending;
	const canOpen = card.canOpen && Boolean(onSend);
	const Row = canOpen ? 'button' : 'div';
	const send = async (text: string) => {
		if (!text || busy || !onSend) return;
		const now = new Date().toISOString();
		setSending(true);
		try {
			await onSend(
				[
					{
						type: 'text',
						id: crypto.randomUUID(),
						text,
						created_at: now,
						finished_at: now,
					},
				],
				{ message_id: messageId, tool_call_id: result.id },
			);
			setReplying(false);
			setDraft('');
		} catch {
			// The normal chat transport reports the error. Keep the draft for retry.
		} finally {
			setSending(false);
		}
	};
	return (
		<section
			aria-label={card.title}
			className="my-2 w-full min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground"
		>
			<header className="flex items-center justify-between gap-2 px-3 py-2">
				<strong className="text-sm">{card.title}</strong>
				<span className="text-xs text-muted-foreground">
					{t('rowCard.count', { count: card.records.length })}
				</span>
			</header>
			{card.records.map((row, index) => (
				<Row
					key={index}
					{...(canOpen ? {
						type: 'button' as const,
						disabled: busy,
						onClick: () => void send(t('rowCard.open', { row: index + 1 })),
					} : {})}
					className="grid min-h-11 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-2 border-t px-3 py-2 text-start enabled:hover:bg-muted focus-visible:bg-muted disabled:opacity-60"
				>
					<span className="text-sm tabular-nums text-muted-foreground">{index + 1}</span>
					<span className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,8rem),1fr))] gap-x-3 gap-y-2">
						{card.columns.map(({ key, label }) => (
							<span key={key} className="min-w-0 text-sm wrap-anywhere">
								<span className="block text-xs text-muted-foreground">{label}</span>
								{String(row[key] ?? '—')}
							</span>
						))}
					</span>
					{canOpen && <ChevronRight className="size-4" aria-hidden="true" />}
				</Row>
			))}
			{canOpen && card.records.length > 0 && (
				<div className="border-t p-1">
					{replying ? (
						<form
							className="flex flex-wrap items-center gap-1 p-1"
							onSubmit={(event) => {
								event.preventDefault();
								void send(draft.trim());
							}}
						>
							<Input
								autoFocus
								aria-label={t('rowCard.reply')}
								placeholder={t('rowCard.placeholder')}
								className="min-w-24 flex-1"
								value={draft}
								disabled={busy}
								onChange={(event) => setDraft(event.target.value)}
							/>
							<Button type="submit" disabled={busy || !draft.trim()}>
								{t('rowCard.send')}
							</Button>
							<Button
								type="button"
								variant="ghost"
								onClick={() => setReplying(false)}
							>
								{t('common.cancel')}
							</Button>
						</form>
					) : (
						<Button
							type="button"
							variant="ghost"
							className="min-h-11"
							disabled={busy}
							onClick={() => setReplying(true)}
						>
							<Reply aria-hidden="true" />
							{t('rowCard.reply')}
						</Button>
					)}
				</div>
			)}
		</section>
	);
}
