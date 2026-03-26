"""Task service - Business logic for tasks"""

from sqlalchemy.orm import Session
from datetime import datetime
from app.models import Task, TaskStatus, Project, SessionTask
from app.schemas import TaskUpdate


class TaskService:
    """Service for task operations"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_task(self, task_id: int):
        """Get a task by ID"""
        return self.db.query(Task).filter(Task.id == task_id).first()
    
    def get_project_tasks(self, project_id: int):
        """Get all tasks for a project"""
        return self.db.query(Task).filter(Task.project_id == project_id).all()
    
    def update_task_status(self, task_id: int, new_status: TaskStatus, error_message: str = None):
        """Update task status with validation"""
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        # Status transition validation
        valid_transitions = {
            TaskStatus.PENDING: [TaskStatus.RUNNING, TaskStatus.CANCELLED],
            TaskStatus.RUNNING: [TaskStatus.DONE, TaskStatus.FAILED],
            TaskStatus.FAILED: [TaskStatus.PENDING],
        }
        
        if new_status not in valid_transitions.get(task.status, []):
            raise ValueError(f"Invalid status transition from {task.status} to {new_status}")
        
        task.status = new_status
        if new_status == TaskStatus.RUNNING:
            task.started_at = datetime.utcnow()
        elif new_status in [TaskStatus.DONE, TaskStatus.FAILED]:
            task.completed_at = datetime.utcnow()
        
        if error_message:
            task.error_message = error_message
        
        self.db.commit()
        self.db.refresh(task)
        return task
    
    def get_next_pending_task(self, project_id: int):
        """Get the next pending task for a project (by priority)"""
        return self.db.query(Task).filter(
            Task.project_id == project_id,
            Task.status == TaskStatus.PENDING
        ).order_by(Task.priority.desc()).first()
    
    def mark_step_complete(self, task_id: int, step_num: int):
        """Mark a step as complete and update current_step"""
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        task.current_step = step_num
        self.db.commit()
        self.db.refresh(task)
        return task
    
    def log_task_event(self, task_id: int, level: str, message: str, metadata: dict = None):
        """Log an event for a task"""
        from app.models import LogEntry
        from app.database import engine
        
        from sqlalchemy import text
        
        # Insert log entry
        log = LogEntry(
            task_id=task_id,
            level=level,
            message=message,
            metadata=metadata
        )
        self.db.add(log)
        self.db.commit()
        return log
