import { useEffect, useState } from "react";
import { describeError, type TeacherProfile } from "@/lib/api";
import {
  Banner, Button, ChipInput, ErrorState, Field, FieldGrid, Icon, NumberInput, Page, PageHeader, Section, Select, SkeletonLines, TextArea, TextInput, useToast,
} from "@/ui";
import { useProfile, useSaveProfile } from "./api";

const LANG = [["", "No default"], ["en-ne", "English + Nepali"], ["en", "English"], ["ne", "नेपाली"]] as const;
const FW = [["", "No default"], ["5E", "5E"], ["gradual_release", "Gradual release"], ["inquiry", "Inquiry"]] as const;

/** `/settings` — defaults that pre-fill every brief, and the voice that flavours every lesson. */
export function ProfilePage() {
  const q = useProfile();
  const save = useSaveProfile();
  const toast = useToast();
  const [p, setP] = useState<TeacherProfile | null>(null);
  useEffect(() => { if (q.data) setP({ local_anchors: [], ...q.data }); }, [q.data]);

  if (q.isPending) return <Page width="narrow"><SkeletonLines lines={6} /></Page>;
  if (q.error || !p) return <Page width="narrow"><ErrorState error={describeError(q.error)} onRetry={() => q.refetch()} /></Page>;

  const set = <K extends keyof TeacherProfile>(k: K, v: TeacherProfile[K]) => setP({ ...p, [k]: v });
  const dirty = JSON.stringify(p) !== JSON.stringify({ local_anchors: [], ...q.data });

  return (
    <Page width="narrow">
      <PageHeader eyebrow="Settings" title="Your teaching profile"
        lede="Anything you leave blank in a brief is filled from here, and your voice and local anchors flavour every lesson so it sounds like you." />
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(p, { onSuccess: () => toast.show("Profile saved") }); }}>
        <Section title="You">
          <FieldGrid>
            <Field label="Name"><TextInput value={p.teacher_name ?? ""} onChange={(e) => set("teacher_name", e.target.value)} /></Field>
            <Field label="School"><TextInput value={p.school ?? ""} onChange={(e) => set("school", e.target.value)} /></Field>
          </FieldGrid>
        </Section>
        <Section title="Defaults" hint="Used when a brief doesn't say.">
          <FieldGrid>
            <Field label="Grade"><NumberInput min={1} max={12} value={p.default_grade} onValue={(v) => set("default_grade", v)} /></Field>
            <Field label="Subject"><TextInput value={p.default_subject ?? ""} onChange={(e) => set("default_subject", e.target.value || null)} /></Field>
            <Field label="Period length">
              <Select value={p.default_duration_min ?? ""} onChange={(e) => set("default_duration_min", e.target.value ? (Number(e.target.value) as 30 | 45 | 60) : null)}>
                <option value="">No default</option><option value="30">30 min</option><option value="45">45 min</option><option value="60">60 min</option>
              </Select>
            </Field>
            <Field label="Language">
              <Select value={p.default_language ?? ""} onChange={(e) => set("default_language", (e.target.value || null) as TeacherProfile["default_language"])}>
                {LANG.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </Select>
            </Field>
            <Field label="Framework">
              <Select value={p.default_framework ?? ""} onChange={(e) => set("default_framework", (e.target.value || null) as TeacherProfile["default_framework"])}>
                {FW.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </Select>
            </Field>
          </FieldGrid>
        </Section>
        <Section title="Voice & local flavour">
          <div style={{ display: "grid", gap: 16 }}>
            <Field label="How you teach" hint="e.g. warm, lots of storytelling and pair work, connect everything to daily life">
              <TextArea autoGrow rows={3} value={p.style_notes ?? ""} onChange={(e) => set("style_notes", e.target.value)} />
            </Field>
            <Field label="Local anchors you like to use" hint="Places, crops, festivals your students know. Enter to add.">
              <div><ChipInput label="Local anchors" values={p.local_anchors ?? []} placeholder="e.g. Phewa lake" onChange={(v) => set("local_anchors", v)} /></div>
            </Field>
          </div>
        </Section>
        {save.error && <Banner tone="danger">{describeError(save.error)}</Banner>}
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!dirty} icon={<Icon.Save size={16} />}>Save profile</Button>
        </div>
      </form>
    </Page>
  );
}
