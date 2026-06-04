"""
Data models for the structured output produced by the LLM extraction step.
These are the intermediate representations before writing to Neo4j.
"""

from __future__ import annotations
from pydantic import BaseModel, Field

# The intended data fields as well as structure for each node/relationship is defined here.
# The output is checked for types, for example in the below class (Sector),
# the sentiment score field is checked to be float or not. If found it is not float, it raises an error,
# and the LLM call is retried until it gets the correct answer/we reach maximum number of retries.

class SectorImpact(BaseModel):
    """A sector and how this event affected it."""

    name: str = Field(
        description="Sector name e.g. 'Financials', 'Energy', 'Healthcare'."
    )
    sentiment_score: float = Field(
        default=0.0,
        description=(
            "Impact score from -1.0 (very negative) to +1.0 (very positive). "
            "Reflects how this event affects the sector overall."
        ),
    )
    impact_reason: str = Field(
        default="", description="One sentence explaining why this score was assigned."
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence in this impact assessment from 0.0 to 1.0. "
            ">0.85 = explicitly stated. 0.6-0.85 = clearly implied. <0.6 = inferred."
        ),
    )


class RegionImpact(BaseModel):
    """A region and how this event affected it."""

    name: str = Field(
        description="Region name e.g. 'Nordic', 'US', 'Europe', 'Middle East'."
    )
    sentiment_score: float = Field(
        default=0.0,
        description=(
            "Impact score from -1.0 (very negative) to +1.0 (very positive). "
            "Reflects how this event affects the region's markets overall."
        ),
    )
    impact_reason: str = Field(
        default="", description="One sentence explaining why this score was assigned."
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence in this impact assessment from 0.0 to 1.0. "
            ">0.85 = explicitly stated. 0.6-0.85 = clearly implied. <0.6 = inferred."
        ),
    )


class CompanyImpact(BaseModel):
    """A company and how this event affected it."""

    name: str = Field(description="Exact company name as it appears in the article.")
    sentiment_score: float = Field(
        default=0.0,
        description=(
            "Impact score from -1.0 (very negative) to +1.0 (very positive). "
            "0.0 = neutral. Be precise — e.g. -0.7 for significant negative impact."
        ),
    )
    impact_reason: str = Field(
        default="", description="One sentence explaining why this score was assigned."
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence in this impact assessment from 0.0 to 1.0. "
            ">0.85 = explicitly stated. 0.6-0.85 = clearly implied. <0.6 = inferred."
        ),
    )


class OrganisationImpact(BaseModel):
    """A non-company organisation and how this event affected it."""

    name: str = Field(
        description=(
            "Name of the organisation — government, central bank, regulator, "
            "international body. e.g. 'Federal Reserve', 'ECB', 'UK Government'."
        )
    )
    sentiment_score: float = Field(
        default=0.0,
        description=(
            "Impact score from -1.0 (very negative) to +1.0 (very positive). "
            "Reflects whether the event aligns with or undermines their mandate/position."
        ),
    )
    impact_reason: str = Field(
        default="", description="One sentence explaining why this score was assigned."
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence in this impact assessment from 0.0 to 1.0. "
            ">0.85 = explicitly stated. 0.6-0.85 = clearly implied. <0.6 = inferred."
        ),
    )


class PersonCompanyImpact(BaseModel):
    """
    A direct causal link: what a specific person said and how it affected a specific company.
    Only populate when the article explicitly connects a person's statement to a company.
    """

    company_name: str = Field(
        description="Exact company name as it appears in the article."
    )
    sentiment_score: float = Field(
        default=0.0,
        description=(
            "How this person's statement specifically affected this company. "
            "-1.0 (very negative) to +1.0 (very positive)."
        ),
    )
    impact_reason: str = Field(
        default="",
        description=(
            "One sentence: what specifically did this person say that affected this company."
        ),
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence that this person's statement directly caused this company impact. "
            ">0.85 = explicitly stated. 0.6-0.85 = clearly implied. <0.6 = inferred."
        ),
    )


class PersonStatement(BaseModel):
    """A named individual, what they said, and the market implication."""

    name: str = Field(
        description="Full name of the individual as it appears in the article."
    )
    role: str = Field(
        default="", description="Their role or title e.g. 'Fed Chair', 'CEO of Nordea'."
    )
    quote_summary: str = Field(
        description=(
            "A concise analytical summary of what they said or did. "
            "e.g. 'Warned that inflation remains sticky and further rate hikes may be needed.'"
        )
    )
    statement_direction: str = Field(
        default="neutral",
        description="Market implication: 'hawkish', 'dovish', 'bullish', 'bearish', 'neutral'.",
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence in attribution from 0.0 to 1.0. "
            ">0.85 = directly quoted. 0.6-0.85 = paraphrased. <0.6 = loosely attributed."
        ),
    )
    company_impacts: list[PersonCompanyImpact] = Field(
        default_factory=list,
        description=(
            "Direct company impacts caused specifically by this person's statement. "
            "Only include if the article explicitly links what this person said to a specific company. "
            "Leave empty if the connection is only indirect via the event."
        ),
    )


class ExtractedEvent(BaseModel):
    """
    A single market event extracted from an article.
    One article may produce 1-N events.
    """

    summary: str = Field(
        description=(
            "A thorough analytical summary of the event — 3-5 sentences. "
            "Include: what happened, who was involved, what was said, "
            "market implications, and which entities are affected and how. "
            "Write this as a financial intelligence briefing paragraph."
        )
    )
    event_type: str = Field(
        default="OTHER",
        description=(
            "Type of market event. Must be one of: "
            "RATE_DECISION, EARNINGS_REPORT, MERGER_ACQUISITION, POLICY_CHANGE, "
            "REGULATORY_ACTION, GEOPOLITICAL_EVENT, ECONOMIC_DATA_RELEASE, "
            "MARKET_MOVEMENT, LEGISLATIVE_PROPOSAL, SANCTIONS, IPO, "
            "LEADERSHIP_CHANGE, OTHER"
        ),
    )
    confidence_score: float = Field(
        default=0.8,
        description=(
            "Confidence this is a genuine distinct market event from 0.0 to 1.0. "
            ">0.85 = clear event with concrete details. "
            "0.6-0.85 = likely event with some ambiguity. "
            "<0.6 = weak signal or background context."
        ),
    )
    asset_classes: list[str] = Field(
        default_factory=list,
        description="e.g. ['Equities', 'Fixed Income', 'Commodities', 'FX', 'Crypto']",
    )
    sectors: list[SectorImpact] = Field(
        default_factory=list,
        description=(
            "Sectors affected by this event, each with their own sentiment score. "
            "A rate hike might score -0.8 for Real Estate but +0.5 for Financials."
        ),
    )
    regions: list[RegionImpact] = Field(
        default_factory=list,
        description=(
            "Regions affected by this event, each with their own sentiment score. "
            "e.g. Nordic, US, Europe, Global, Asia, Middle East, UK, China."
        ),
    )
    themes: list[str] = Field(
        default_factory=list,
        description="e.g. ['Inflation', 'Volatility', 'Interest Rates', 'Geopolitical Risk']",
    )
    companies: list[CompanyImpact] = Field(
        default_factory=list,
        description=(
            "Named private companies or listed corporations with individual sentiment scores."
        ),
    )
    organisations: list[OrganisationImpact] = Field(
        default_factory=list,
        description=(
            "Non-company organisations with individual sentiment scores. "
            "Do NOT duplicate entries already in companies."
        ),
    )
    persons: list[PersonStatement] = Field(
        default_factory=list,
        description=(
            "Named individuals who made statements or took actions relevant to this event."
        ),
    )
    overall_direction: str = Field(
        default="neutral",
        description="Overall market sentiment: 'positive', 'negative', or 'neutral'.",
    )
    embedding: list[float] = Field(
        default_factory=list,
        description="Vector embedding of the event summary for semantic search.",
    )


class ArticleExtractionResult(BaseModel):
    """Full extraction result for one article."""

    article_id: int
    events: list[ExtractedEvent]
    embedding: list[float] = Field(
        default_factory=list,
        description="Vector embedding of the full article content.",
    )

# The description for each node/relationship model is provided here.