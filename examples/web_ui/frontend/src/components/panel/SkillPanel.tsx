import { useQuery } from '@tanstack/react-query';
import { BriefcaseBusiness, FileX, PlusCircle, RefreshCw, Search, SearchX, Trash } from 'lucide-react';
import { useState } from 'react';

import type { Skill } from '@/api';
import { ApiError } from '@/api/client';
import { enterpriseSkillKey, listEnterpriseSkills } from '@/api/enterpriseBusiness';
import type { UploadOptions } from '@/api/workspace';
import { AddSkillDialog } from '@/components/dialog/AddSkillDialog.tsx';
import { DeleteDialog } from '@/components/dialog/DeleteDialog.tsx';
import { PanelEmpty } from '@/components/panel/PanelEmpty';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar.tsx';
import { Button } from '@/components/ui/button';
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group';
import {
	Item,
	ItemActions,
	ItemContent,
	ItemDescription,
	ItemMedia,
	ItemTitle,
} from '@/components/ui/item';
import { useSkills } from '@/hooks/useSkills.ts';
import { useTranslation } from '@/i18n/useI18n.ts';

interface SkillPanelProps {
	agentId: string | null;
	sessionId: string | null;
	/** The skills equipped in the workspace. */
	skills: Skill[];
	/** Whether the skill list is still loading. */
	loading?: boolean;
	/**
	 * Upload a picked folder as a skill.
	 *
	 * @param files - The folder's files, carrying `webkitRelativePath`.
	 * @param options - Progress and abort hooks.
	 */
	onUpload: (files: File[], options?: UploadOptions) => Promise<void>;
	/**
	 * Install skills the user already has.
	 *
	 * @param skillIds - The library record ids to add.
	 */
	onAddFromLibrary: (skillIds: string[]) => Promise<void>;
	/**
	 * Remove a skill by name.
	 *
	 * @param name - The skill name to remove.
	 */
	onRemove: (name: string) => Promise<void>;
}

/**
 * Lists workspace skills and the current Agent's registered business skills.
 *
 * Renders without its own header/border — the surrounding `Panel`
 * chrome (from `PanelDock`) provides those.
 *
 * @param skills - The skills to list.
 * @param loading - Whether the list is loading.
 * @param onUpload - Folder-upload callback.
 * @param onAddFromLibrary - Library-install callback.
 * @param onRemove - Remove-skill callback.
 * @returns The skill panel body.
 */
export function SkillPanel({
	agentId,
	sessionId,
	skills,
	loading = false,
	onUpload,
	onAddFromLibrary,
	onRemove,
}: SkillPanelProps) {
	const { t } = useTranslation();
	const [search, setSearch] = useState('');
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
	// The workspace stores only the skill itself, so the icon and author
	// are looked up in the library, matched on the shared name.
	const { skills: library } = useSkills();
	const byName = new Map(library.map((skill) => [skill.name, skill]));

	const business = useQuery({
		queryKey: [...enterpriseSkillKey(agentId), sessionId],
		enabled: Boolean(agentId),
		queryFn: ({ signal }) => listEnterpriseSkills(agentId!, sessionId, signal),
		staleTime: 0,
		retry: false,
		refetchOnWindowFocus: true,
	});
	const query = search.trim().toLocaleLowerCase();
	const filtered = skills.filter((skill) => skill.name.toLocaleLowerCase().includes(query));
	const businessSkills = (business.isError ? [] : business.data ?? []).filter((skill) =>
		[skill.display_name, skill.name, skill.description, skill.business, t('panel.skill.actions.' + skill.action)]
			.some((value) => value.toLocaleLowerCase().includes(query)),
	);
	const showBusiness = business.isSuccess && (!query || businessSkills.length > 0);

	return (
		<div className="flex flex-col flex-1 min-h-0 gap-y-2">
			<span className="text-muted-foreground text-sm">{t('panel.skill.description')}</span>
			<InputGroup>
				<InputGroupInput
					placeholder={t('panel.skill.searchPlaceholder')}
					value={search}
					onChange={(e) => setSearch(e.target.value)}
				/>
				<InputGroupAddon align="inline-end">
					<Search />
				</InputGroupAddon>
				{agentId && (
					<InputGroupAddon align="inline-end">
						<Button variant="ghost" size="icon-sm" disabled={business.isFetching}
							aria-label={t('panel.skill.refreshBusiness')} title={t('panel.skill.refreshBusiness')}
							onClick={() => void business.refetch()}>
							<RefreshCw className="size-3.5" />
						</Button>
					</InputGroupAddon>
				)}
			</InputGroup>
			{business.isError && (
				<p role="alert" className="text-sm text-destructive">
					{t(business.error instanceof ApiError && business.error.status === 401
						? 'panel.skill.businessAuthError'
						: 'panel.skill.businessLoadError')}
				</p>
			)}

			{(loading || business.isLoading) && filtered.length === 0 && businessSkills.length === 0 ? (
				<div className="flex flex-1 items-center justify-center">
					<p className="text-muted-foreground text-sm">{t('panel.loading')}</p>
				</div>
			) : filtered.length === 0 && !showBusiness && !business.isError ? (
				<PanelEmpty
					icon={search ? SearchX : FileX}
					title={search ? t('panel.search.emptyTitle') : t('panel.skill.emptyTitle')}
					description={
						search
							? t('panel.search.emptyDescription', { query: search })
							: t('panel.skill.emptyDescription')
					}
				/>
			) : (
				<div className="flex flex-col flex-1 min-h-0 overflow-y-auto scroll-fade gap-y-2">
					{showBusiness && (
						<h3 className="text-xs font-medium text-muted-foreground">
							{t('panel.skill.business')} · {businessSkills.length}
						</h3>
					)}
					{showBusiness && businessSkills.length === 0 && (
						<p className="text-sm text-muted-foreground">{t('panel.skill.businessEmpty')}</p>
					)}
					{businessSkills.map((skill) => (
						<Item key={'business:' + skill.name} variant="outline">
							<ItemMedia><BriefcaseBusiness className="size-4" /></ItemMedia>
							<ItemContent className="min-w-0">
								<ItemTitle className="break-words">{skill.display_name}</ItemTitle>
								{skill.description && skill.description !== skill.display_name && (
									<ItemDescription className="line-clamp-2">{skill.description}</ItemDescription>
								)}
								<span className="break-all text-xs text-muted-foreground">
									{t('panel.skill.actions.' + skill.action)} · {skill.name}
								</span>
							</ItemContent>
						</Item>
					))}
					{showBusiness && filtered.length > 0 && (
						<h3 className="text-xs font-medium text-muted-foreground">{t('panel.skill.workspaceSkills')}</h3>
					)}
					{filtered.map((skill) => {
						const installed = byName.get(skill.name);
						return (
							<Item key={skill.name} variant="outline" className="group/skill">
								<ItemMedia>
									<Avatar className="rounded-md">
										<AvatarImage
											src={installed?.icon_url ?? undefined}
											alt={skill.name}
											loading="lazy"
										/>
										<AvatarFallback className="rounded-md">
											{skill.name.slice(0, 1).toUpperCase()}
										</AvatarFallback>
									</Avatar>
								</ItemMedia>
								<ItemContent>
									<ItemTitle>
										<span className="truncate font-medium">{skill.name}</span>
										{installed?.author && (
											<span className="text-xs text-muted-foreground">
												@{installed.author}
											</span>
										)}
									</ItemTitle>
									<ItemDescription className="line-clamp-2">
										{skill.description}
									</ItemDescription>
								</ItemContent>
								<ItemActions>
									{/* Only on hover: deleting is rare, and a
									    button on every row competes with the
									    content for attention. */}
									<Button
										variant="secondary"
										size="icon-sm"
										className="opacity-0 transition-opacity group-hover/skill:opacity-100 focus-visible:opacity-100"
										onClick={() => {
											setDeleteTarget(skill.name);
											setDeleteOpen(true);
										}}
										title={t('common.delete')}
									>
										<Trash className="size-3" />
									</Button>
								</ItemActions>
							</Item>
						);
					})}
				</div>
			)}

			<AddSkillDialog
				present={new Set(skills.map((s) => s.name))}
				onUpload={onUpload}
				onAddFromLibrary={onAddFromLibrary}
			>
				<Button variant="default">
					<PlusCircle />
					{t('panel.skill.add')}
				</Button>
			</AddSkillDialog>

			<DeleteDialog
				open={deleteOpen}
				onOpenChange={setDeleteOpen}
				title={t('common.deleteTitle', {
					entity: t('dialog-mcp-delete.skillEntity'),
					name: deleteTarget ?? '',
				})}
				description={t('dialog-mcp-delete.skillDescription')}
				onConfirm={async () => {
					if (deleteTarget) await onRemove(deleteTarget);
				}}
			/>
		</div>
	);
}
