"use client";

/**
 * 技能详情路由 /skills
 *
 * 左侧 UnifiedSidebar（技能安装 + 列表）+ 右侧 SkillDetail（SKILL.md 详情）。
 * selectedSkill 状态在此页持有，通过 props 下传给侧栏和主区。
 */

import { useState } from "react";
import { UnifiedSidebar } from "@/components/layout/UnifiedSidebar";
import { SkillDetail } from "@/components/skills/SkillDetail";

export default function SkillsPage() {
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);

  return (
    <div className="flex h-full w-full">
      <UnifiedSidebar
        onSkillSelect={setSelectedSkill}
        selectedSkill={selectedSkill}
      />
      <main className="flex min-w-0 flex-1">
        <SkillDetail skillName={selectedSkill} />
      </main>
    </div>
  );
}