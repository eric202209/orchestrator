"""Retry configuration and utilities for Celery tasks"""

import logging
from typing import Optional, Dict, Any
from functools import wraps
from celery.exceptions import Retry
import time

logger = logging.getLogger(__name__)


class RetryConfig:
    """Retry configuration settings"""
    
    # Default retry settings
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_RETRY_DELAY = 60  # seconds
    
    # Task-specific retry settings
    TASK_RETRY_SETTINGS = {
        "execute_openclaw_task": {
            "max_retries": 3,
            "retry_delay": 60,
            "retry_on": [Exception],  # Retry on any exception
        },
        "process_github_webhook": {
            "max_retries": 3,
            "retry_delay": 30,
            "retry_on": [Exception],
        },
        "scheduled_task_execution": {
            "max_retries": 5,
            "retry_delay": 30,
            "retry_on": [Exception],
        },
        "cleanup_old_logs": {
            "max_retries": 3,
            "retry_delay": 120,
            "retry_on": [Exception],
        },
    }
    
    @classmethod
    def get_retry_config(cls, task_name: str) -> Dict[str, Any]:
        """Get retry configuration for a task"""
        return cls.TASK_RETRY_SETTINGS.get(
            task_name,
            {
                "max_retries": cls.DEFAULT_MAX_RETRIES,
                "retry_delay": cls.DEFAULT_RETRY_DELAY,
                "retry_on": [Exception],
            }
        )


def with_retry(task_func, max_retries: Optional[int] = None, retry_delay: Optional[int] = None):
    """
    Decorator to add retry logic to any function
    
    Args:
        task_func: Function to wrap
        max_retries: Maximum retry attempts
        retry_delay: Delay between retries in seconds
        
    Returns:
        Wrapped function with retry logic
    """
    @wraps(task_func)
    def wrapper(*args, **kwargs):
        retry_count = kwargs.pop('retry_count', 0)
        max_retries = max_retries or RetryConfig.DEFAULT_MAX_RETRIES
        retry_delay = retry_delay or RetryConfig.DEFAULT_RETRY_DELAY
        
        try:
            return task_func(*args, **kwargs)
            
        except Exception as e:
            if retry_count < max_retries:
                logger.warning(
                    f"Task failed, retrying ({retry_count + 1}/{max_retries}): {str(e)}"
                )
                time.sleep(retry_delay)
                return wrapper(*args, retry_count=retry_count + 1, **kwargs)
            else:
                logger.error(
                    f"Task failed after {max_retries} retries: {str(e)}"
                )
                raise
    
    return wrapper


def exponential_backoff_delay(attempt: int, base_delay: int = 60, max_delay: int = 3600) -> int:
    """
    Calculate delay with exponential backoff
    
    Args:
        attempt: Current attempt number (1-indexed)
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds
        
    Returns:
        Delay in seconds
    """
    import random
    
    # Exponential backoff with jitter
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    
    # Add jitter (±25%)
    jitter = delay * 0.25 * (random.random() * 2 - 1)
    delay += jitter
    
    return max(0, int(delay))


class RetryableError(Exception):
    """Custom exception for retryable errors"""
    
    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after  # Seconds to wait before retry


class NonRetryableError(Exception):
    """Custom exception for non-retryable errors"""
    pass


def should_retry_exception(exc: Exception) -> bool:
    """
    Determine if an exception should be retried
    
    Args:
        exc: Exception to check
        
    Returns:
        True if should retry, False otherwise
    """
    # Retry network errors, timeouts, etc.
    retryable_exceptions = (
        ConnectionError,
        TimeoutError,
        Retry,
        RetryableError,
    )
    
    # Don't retry validation errors, not found, etc.
    non_retryable_exceptions = (
        ValueError,
        KeyError,
        TypeError,
        NonRetryableError,
    )
    
    if isinstance(exc, non_retryable_exceptions):
        return False
    
    if isinstance(exc, retryable_exceptions):
        return True
    
    # Default: retry on most exceptions
    return True
