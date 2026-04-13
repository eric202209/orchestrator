package com.user.ui

import android.content.Context
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.text.style.TypefaceSpan
import androidx.core.content.ContextCompat
import com.user.R

object OutputHighlighter {

    private val jsonKeyRegex = Regex("\"([^\"]+)\"(?=\\s*:)")
    private val jsonStringRegex = Regex("(?<=:\\s)\"([^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"")
    private val numberRegex = Regex("(?<![\\w.-])-?\\d+(?:\\.\\d+)?(?![\\w.-])")
    private val booleanRegex = Regex("\\b(true|false|null)\\b", RegexOption.IGNORE_CASE)
    private val logTagRegex = Regex("\\[(INFO|WARN|WARNING|ERROR|DEBUG|PERFORMANCE|ORCHESTRATION|OPENCLAW|CHECKPOINT)\\]")
    private val keywordRegex = Regex(
        "\\b(FAILED|FAILURE|ERROR|COMPLETED|DONE|SUCCESS|RUNNING|PENDING|TIMEOUT|RETRY|RESUME|CHECKPOINT)\\b",
        RegexOption.IGNORE_CASE
    )
    private val inlineCodeRegex = Regex("`([^`]+)`")

    fun render(context: Context, raw: String, isError: Boolean = false): CharSequence {
        val text = raw.trimEnd()
        if (text.isBlank()) return ""
        if (text.contains("```")) {
            return MarkdownRenderer.render(context, text)
        }

        val builder = SpannableStringBuilder(text)
        applyMonospace(builder, 0, builder.length)

        applyRegexSpan(builder, jsonKeyRegex) {
            ForegroundColorSpan(ContextCompat.getColor(context, R.color.status_running))
        }
        applyRegexSpan(builder, jsonStringRegex) {
            ForegroundColorSpan(ContextCompat.getColor(context, R.color.code_text))
        }
        applyRegexSpan(builder, numberRegex) {
            ForegroundColorSpan(ContextCompat.getColor(context, R.color.status_pending))
        }
        applyRegexSpan(builder, booleanRegex) {
            ForegroundColorSpan(ContextCompat.getColor(context, R.color.status_timeout))
        }
        applyRegexSpan(builder, inlineCodeRegex) {
            ForegroundColorSpan(ContextCompat.getColor(context, R.color.code_text))
        }

        logTagRegex.findAll(text).forEach { match ->
            val color = when (match.groupValues[1].uppercase()) {
                "ERROR" -> R.color.status_failed
                "WARN", "WARNING" -> R.color.status_pending
                "DEBUG" -> R.color.text_secondary
                "PERFORMANCE" -> R.color.status_timeout
                "ORCHESTRATION", "OPENCLAW", "CHECKPOINT" -> R.color.status_running
                else -> R.color.status_completed
            }
            applyColorSpan(builder, context, color, match.range.first, match.range.last + 1)
            builder.setSpan(
                StyleSpan(Typeface.BOLD),
                match.range.first,
                match.range.last + 1,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
            )
        }

        keywordRegex.findAll(text).forEach { match ->
            val word = match.value.uppercase()
            val color = when (word) {
                "FAILED", "FAILURE", "ERROR", "TIMEOUT" -> R.color.status_failed
                "COMPLETED", "DONE", "SUCCESS" -> R.color.status_completed
                "RUNNING", "RESUME" -> R.color.status_running
                "PENDING", "RETRY" -> R.color.status_pending
                "CHECKPOINT" -> R.color.status_timeout
                else -> if (isError) R.color.status_failed else R.color.text_primary
            }
            applyColorSpan(builder, context, color, match.range.first, match.range.last + 1)
        }

        return builder
    }

    private fun applyMonospace(
        builder: SpannableStringBuilder,
        start: Int,
        end: Int,
    ) {
        builder.setSpan(
            TypefaceSpan("monospace"),
            start,
            end,
            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
        )
    }

    private fun applyColorSpan(
        builder: SpannableStringBuilder,
        context: Context,
        colorRes: Int,
        start: Int,
        end: Int,
    ) {
        builder.setSpan(
            ForegroundColorSpan(ContextCompat.getColor(context, colorRes)),
            start,
            end,
            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
        )
    }

    private fun applyRegexSpan(
        builder: SpannableStringBuilder,
        regex: Regex,
        spanFactory: () -> Any,
    ) {
        regex.findAll(builder).forEach { match ->
            builder.setSpan(
                spanFactory(),
                match.range.first,
                match.range.last + 1,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
            )
        }
    }
}