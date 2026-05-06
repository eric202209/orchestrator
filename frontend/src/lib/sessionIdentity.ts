import type { Session } from '@/types/api';

const normalize = (value: string) => value.trim().toLowerCase().replace(/\s+/g, ' ');

export const isLegacyTaskExecutionSession = (
  session: Session,
  taskTitles: string[] = []
): boolean => {
  if (session.instance_id?.startsWith('orchestrator-task-')) {
    return true;
  }

  const sessionName = normalize(session.name || '');
  if (/^task \d+ execution$/.test(sessionName)) {
    return true;
  }

  return taskTitles.some((title) => {
    const taskSessionName = `${normalize(title)} session`;
    return sessionName === taskSessionName || sessionName.startsWith(`${taskSessionName}-`);
  });
};

