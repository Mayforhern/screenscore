"""Evidence registry, dependency graph, candidate classification, and claim validation.

This module implements programmatic evidence-dependency validation to prevent
the agent from promoting partially-qualified candidates to "strict comparables"
when the target's genres are unverified (e.g., target absent from ClickHouse).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class EvidenceStatus(str, Enum):
    """Status of evidence item."""
    VERIFIED = "verified"
    DERIVED = "derived"
    NOT_VERIFIED = "not_verified"
    NOT_COMPUTABLE = "not_computable"
    CONTRADICTED = "contradicted"


class CandidateClass(str, Enum):
    """Classification of a candidate title."""
    STRICT_COMPARABLE = "strict_comparable"
    PARTIAL_MATCH = "partial_match"
    CANDIDATE = "candidate"
    FALLBACK_MATCH = "fallback_match"
    UNVERIFIABLE = "unverifiable"


class ClaimStatus(str, Enum):
    """Status of an analytical claim."""
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    GATED = "gated"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"


@dataclass
class EvidenceItem:
    """A single piece of evidence collected during research."""
    key: str
    description: str
    status: EvidenceStatus
    source_query: Optional[str] = None
    source_tool: Optional[str] = None
    value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self) -> None:
        """Validate evidence item fields on construction."""
        if not self.key or not self.key.strip():
            raise ValueError("EvidenceItem key must be a non-empty string")
        if not self.description:
            raise ValueError("EvidenceItem description must be a non-empty string")

    def is_available(self) -> bool:
        """Check if this evidence is usable for claims."""
        return self.status in (EvidenceStatus.VERIFIED, EvidenceStatus.DERIVED)


@dataclass
class EvidenceClaim:
    """An analytical claim that depends on evidence."""
    claim_id: str
    description: str
    required_evidence: List[str]
    status: ClaimStatus = ClaimStatus.UNKNOWN
    gated_by: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def can_be_supported(self, evidence_statuses: Dict[str, EvidenceStatus]) -> bool:
        """Check if all required evidence is available."""
        for key in self.required_evidence:
            status = evidence_statuses.get(key, EvidenceStatus.NOT_COMPUTABLE)
            if status not in (EvidenceStatus.VERIFIED, EvidenceStatus.DERIVED):
                return False
        return True


class EvidenceRegistry:
    """Registry managing evidence items, dependencies, and claims."""
    
    def __init__(self) -> None:
        self.items: Dict[str, EvidenceItem] = {}
        self.claims: Dict[str, EvidenceClaim] = {}
        self.dependencies: Dict[str, List[str]] = {}
        self.candidate_classifications: Dict[str, CandidateClass] = {}
    
    def record_evidence(
        self,
        key: str,
        description: str,
        status: EvidenceStatus,
        source_query: Optional[str] = None,
        source_tool: Optional[str] = None,
        value: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EvidenceItem:
        """Record a new evidence item."""
        item = EvidenceItem(
            key=key,
            description=description,
            status=status,
            source_query=source_query,
            source_tool=source_tool,
            value=value,
            metadata=metadata or {},
        )
        self.items[key] = item
        
        # Check if any dependencies are contradicted/not_computable
        for dependency_key in self._get_dependencies(key):
            dep_status = self.items.get(dependency_key, EvidenceItem(
                key=dependency_key,
                description="[sentinel — dependency not yet recorded]",
                status=EvidenceStatus.NOT_COMPUTABLE,
            )).status

            if dep_status in (EvidenceStatus.CONTRADICTED, EvidenceStatus.NOT_COMPUTABLE):
                item.status = dep_status
                break
        
        # Propagate status to dependents
        self._propagate_status(key, item.status)
        return item
    
    def _get_dependencies(self, key: str) -> List[str]:
        """Get all keys that this key depends on."""
        dependencies = []
        for dep_key, dependents in self.dependencies.items():
            if key in dependents:
                dependencies.append(dep_key)
        return dependencies
    
    def _propagate_status(self, key: str, status: EvidenceStatus) -> None:
        """Propagate status changes through dependency graph."""
        if status in (EvidenceStatus.CONTRADICTED, EvidenceStatus.NOT_COMPUTABLE):
            for dependent_key in self.dependencies.get(key, []):
                if dependent_key in self.items:
                    self.items[dependent_key].status = status
                    self._propagate_status(dependent_key, status)
    
    def add_dependency(self, dependent: str, dependency: str) -> None:
        """Add a dependency relationship (dependent depends on dependency)."""
        if dependency not in self.dependencies:
            self.dependencies[dependency] = []
        self.dependencies[dependency].append(dependent)
    
    def record_claim(
        self,
        claim_id: str,
        description: str,
        required_evidence: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EvidenceClaim:
        """Record an analytical claim."""
        claim = EvidenceClaim(
            claim_id=claim_id,
            description=description,
            required_evidence=required_evidence,
            metadata=metadata or {},
        )
        self.claims[claim_id] = claim
        self._validate_claim(claim)
        return claim
    
    def _validate_claim(self, claim: EvidenceClaim) -> ClaimStatus:
        """Validate a claim against current evidence."""
        evidence_statuses = {
            key: item.status for key, item in self.items.items()
        }
        
        gated_by = []
        for required_key in claim.required_evidence:
            status = evidence_statuses.get(required_key, EvidenceStatus.NOT_COMPUTABLE)
            if status not in (EvidenceStatus.VERIFIED, EvidenceStatus.DERIVED):
                gated_by.append(required_key)
        
        if not gated_by:
            claim.status = ClaimStatus.SUPPORTED
            claim.gated_by = []
        else:
            claim.status = ClaimStatus.GATED
            claim.gated_by = gated_by
        
        return claim.status
    
    def validate_all_claims(self) -> Dict[str, ClaimStatus]:
        """Validate all claims and return their statuses."""
        results = {}
        for claim in self.claims.values():
            self._validate_claim(claim)
            results[claim.claim_id] = claim.status
        return results
    
    def classify_candidate(
        self,
        candidate_id: str,
        has_genre_overlap: bool,
        has_entity_match: bool,
        target_genres_verified: bool,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CandidateClass:
        """Classify a candidate title based on evidence."""
        if not target_genres_verified:
            classification = CandidateClass.UNVERIFIABLE
        elif has_genre_overlap and has_entity_match:
            classification = CandidateClass.STRICT_COMPARABLE
        elif has_genre_overlap or has_entity_match:
            classification = CandidateClass.PARTIAL_MATCH
        else:
            classification = CandidateClass.CANDIDATE
        
        self.candidate_classifications[candidate_id] = classification
        return classification
    
    def get_evidence_status(self, key: str) -> Optional[EvidenceStatus]:
        """Get the status of an evidence item."""
        item = self.items.get(key)
        return item.status if item else None
    
    def get_audit_summary(self) -> Dict[str, Any]:
        """Get summary of evidence and claims for audit."""
        return {
            "total_evidence": len(self.items),
            "verified_evidence": sum(
                1 for item in self.items.values()
                if item.status == EvidenceStatus.VERIFIED
            ),
            "derived_evidence": sum(
                1 for item in self.items.values()
                if item.status == EvidenceStatus.DERIVED
            ),
            "not_verified_evidence": sum(
                1 for item in self.items.values()
                if item.status == EvidenceStatus.NOT_VERIFIED
            ),
            "total_claims": len(self.claims),
            "supported_claims": sum(
                1 for claim in self.claims.values()
                if claim.status == ClaimStatus.SUPPORTED
            ),
            "gated_claims": sum(
                1 for claim in self.claims.values()
                if claim.status == ClaimStatus.GATED
            ),
            "candidate_classifications": {
                candidate_id: getattr(classification, "value", str(classification))
                for candidate_id, classification in self.candidate_classifications.items()
            },
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert registry state to dictionary for pipeline storage."""
        return {
            "items": {
                key: {
                    "key": item.key,
                    "description": item.description,
                    "status": getattr(item.status, "value", str(item.status)),
                    "source_query": item.source_query,
                    "source_tool": item.source_tool,
                    "value": item.value,
                    "metadata": item.metadata,
                }
                for key, item in self.items.items()
            },
            "claims": {
                claim_id: {
                    "claim_id": claim.claim_id,
                    "description": claim.description,
                    "required_evidence": claim.required_evidence,
                    "status": getattr(claim.status, "value", str(claim.status)),
                    "gated_by": claim.gated_by,
                    "metadata": claim.metadata,
                }
                for claim_id, claim in self.claims.items()
            },
            "dependencies": self.dependencies,
            "candidate_classifications": {
                key: getattr(value, "value", str(value))
                for key, value in self.candidate_classifications.items()
            },
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvidenceRegistry":
        """Restore registry state from dictionary."""
        registry = cls()
        
        for key, item_data in data.get("items", {}).items():
            try:
                status = EvidenceStatus(item_data["status"])
            except ValueError:
                status = EvidenceStatus.NOT_VERIFIED
            registry.items[key] = EvidenceItem(
                key=item_data["key"],
                description=item_data["description"],
                status=status,
                source_query=item_data.get("source_query"),
                source_tool=item_data.get("source_tool"),
                value=item_data.get("value"),
                metadata=item_data.get("metadata", {}),
            )
        
        for claim_id, claim_data in data.get("claims", {}).items():
            try:
                status = ClaimStatus(claim_data["status"])
            except ValueError:
                status = ClaimStatus.UNKNOWN
            registry.claims[claim_id] = EvidenceClaim(
                claim_id=claim_data["claim_id"],
                description=claim_data["description"],
                required_evidence=claim_data["required_evidence"],
                status=status,
                gated_by=claim_data.get("gated_by", []),
                metadata=claim_data.get("metadata", {}),
            )
        
        registry.dependencies = data.get("dependencies", {})
        
        for key, class_value in data.get("candidate_classifications", {}).items():
            try:
                registry.candidate_classifications[key] = CandidateClass(class_value)
            except ValueError:
                pass
        
        return registry

