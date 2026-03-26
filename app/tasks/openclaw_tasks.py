"""OpenClaw-specific Celery tasks"""

import logging
from typing import Optional, Dict, Any, List
from app.celery_app import celery_app
from app.tasks.worker import get_db_session
from app.models import Session as SessionModel, Task, TaskStatus
from app.services import OpenClawSessionService, PromptTemplates

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def execute_openclaw_code_generation(
    self,
    session_id: int,
    task_id: int,
    code_description: str,
    file_path: str,
    language: str = "python"
):
    """
    Execute OpenClaw to generate code
    
    Args:
        session_id: Session ID
        task_id: Task ID
        code_description: What code to generate
        file_path: Where to save the code
        language: Programming language
    """
    db = get_db_session()
    
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        # Update status
        task.status = TaskStatus.RUNNING
        db.commit()
        
        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, task_id)
        
        # Build prompt for code generation
        prompt = PromptTemplates.render(
            "code_implementation",
            implementation_task=code_description,
            current_context=f"File: {file_path}, Language: {language}",
            files_to_modify=file_path,
            constraints="Production-ready code with error handling and tests"
        )
        
        # Execute
        result = openclaw_service.execute_task(prompt, timeout_seconds=600)
        
        # Update task
        task.status = TaskStatus.DONE
        task.completed_at = Task.completed_at or None  # Would be set by service
        db.commit()
        
        return {
            "status": "completed",
            "file_path": file_path,
            "language": language,
            "result": result
        }
        
    except Exception as exc:
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        
        logger.error(f"Code generation failed: {str(exc)}")
        raise self.retry(exc=exc)
        
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def execute_openclaw_debugging(
    self,
    session_id: int,
    task_id: int,
    error_message: str,
    stack_trace: str,
    reproduction_steps: str
):
    """
    Execute OpenClaw to debug an issue
    
    Args:
        session_id: Session ID
        task_id: Task ID
        error_message: Error to debug
        stack_trace: Stack trace
        reproduction_steps: How to reproduce
    """
    db = get_db_session()
    
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        task.status = TaskStatus.RUNNING
        db.commit()
        
        openclaw_service = OpenClawSessionService(db, session_id, task_id)
        
        # Build debugging prompt
        prompt = PromptTemplates.render(
            "debugging",
            error_report=error_message,
            stack_trace=stack_trace,
            reproduction_steps=reproduction_steps,
            code_context=""  # Would be filled with actual code context
        )
        
        result = openclaw_service.execute_task(prompt, timeout_seconds=600)
        
        task.status = TaskStatus.DONE
        db.commit()
        
        return {
            "status": "completed",
            "diagnosis": result.get("diagnosis"),
            "fix": result.get("fix")
        }
        
    except Exception as exc:
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        
        raise self.retry(exc=exc)
        
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def execute_openclaw_testing(
    self,
    session_id: int,
    task_id: int,
    component_name: str,
    test_type: str = "unit"
):
    """
    Execute OpenClaw to generate tests
    
    Args:
        session_id: Session ID
        task_id: Task ID
        component_name: Component to test
        test_type: Type of test (unit, integration, e2e)
    """
    db = get_db_session()
    
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        task.status = Task.RUNNING
        db.commit()
        
        openclaw_service = OpenClawSessionService(db, session_id, task_id)
        
        # Build testing prompt
        prompt = PromptTemplates.render(
            "testing",
            feature_description=component_name,
            test_context=f"Test type: {test_type}"
        )
        
        result = openclaw_service.execute_task(prompt, timeout_seconds=600)
        
        task.status = TaskStatus.DONE
        db.commit()
        
        return {
            "status": "completed",
            "test_plan": result.get("test_plan"),
            "test_cases": result.get("test_cases")
        }
        
    except Exception as exc:
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        
        raise self.retry(exc=exc)
        
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def execute_openclaw_code_review(
    self,
    session_id: int,
    task_id: int,
    code_changes: str,
    review_criteria: Optional[List[str]] = None
):
    """
    Execute OpenClaw for code review
    
    Args:
        session_id: Session ID
        task_id: Task ID
        code_changes: Code to review
        review_criteria: Review criteria
    """
    db = get_db_session()
    
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        task.status = TaskStatus.RUNNING
        db.commit()
        
        openclaw_service = OpenClawSessionService(db, session_id, task_id)
        
        # Build code review prompt
        prompt = PromptTemplates.render(
            "code_review",
            code_changes=code_changes,
            review_criteria="\n".join(review_criteria or [])
        )
        
        result = openclaw_service.execute_task(prompt, timeout_seconds=600)
        
        task.status = TaskStatus.DONE
        db.commit()
        
        return {
            "status": "completed",
            "review": result.get("review"),
            "recommendations": result.get("recommendations")
        }
        
    except Exception as exc:
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        
        raise self.retry(exc=exc)
        
    finally:
        db.close()
