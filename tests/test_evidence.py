"""Tests for evidence dependency tracking system."""

import pytest

from screenscore.evidence import (
    CandidateClass,
    ClaimStatus,
    EvidenceClaim,
    EvidenceItem,
    EvidenceRegistry,
    EvidenceStatus,
)


class TestEvidenceRegistry:
    """Test EvidenceRegistry core functionality."""

    def test_record_evidence_verified(self):
        """EVID-1: Record verified evidence item."""
        registry = EvidenceRegistry()
        item = registry.record_evidence(
            key="target_genres",
            description="Target genres verified via Q4",
            status=EvidenceStatus.VERIFIED,
            source_query="Q4",
        )
        assert item.key == "target_genres"
        assert item.status == EvidenceStatus.VERIFIED
        assert item.is_available()

    def test_record_evidence_not_verified(self):
        """EVID-2: Record not_verified evidence item."""
        registry = EvidenceRegistry()
        item = registry.record_evidence(
            key="target_genres",
            description="Target absent from ClickHouse",
            status=EvidenceStatus.NOT_VERIFIED,
        )
        assert item.status == EvidenceStatus.NOT_VERIFIED
        assert not item.is_available()

    def test_classify_candidate_strict_comparable(self):
        """EVID-3: Classify candidate as strict_comparable when all prerequisites met."""
        registry = EvidenceRegistry()
        classification = registry.classify_candidate(
            candidate_id="comp_1",
            has_genre_overlap=True,
            has_entity_match=True,
            target_genres_verified=True,
        )
        assert classification == CandidateClass.STRICT_COMPARABLE
        assert registry.candidate_classifications["comp_1"] == CandidateClass.STRICT_COMPARABLE

    def test_classify_candidate_unverifiable_when_target_absent(self):
        """EVID-4: Classify candidate as UNVERIFIABLE when target genres not verified."""
        registry = EvidenceRegistry()
        classification = registry.classify_candidate(
            candidate_id="comp_1",
            has_genre_overlap=True,
            has_entity_match=True,
            target_genres_verified=False,
        )
        assert classification == CandidateClass.UNVERIFIABLE

    def test_validate_claim_supported(self):
        """EVID-5: Validate claim as supported when all required evidence is verified."""
        registry = EvidenceRegistry()
        registry.record_evidence(
            key="target_genres",
            description="Target genres verified",
            status=EvidenceStatus.VERIFIED,
        )
        registry.record_evidence(
            key="genre_overlap",
            description="Genre overlap confirmed",
            status=EvidenceStatus.VERIFIED,
        )
        claim = registry.record_claim(
            claim_id="strict_comparable",
            description="Candidate is a strict comparable",
            required_evidence=["target_genres", "genre_overlap"],
        )
        assert claim.status == ClaimStatus.SUPPORTED
        assert claim.gated_by == []

    def test_validate_claim_gated(self):
        """EVID-6: Validate claim as gated when required evidence is missing."""
        registry = EvidenceRegistry()
        registry.record_evidence(
            key="target_genres",
            description="Target absent from ClickHouse",
            status=EvidenceStatus.NOT_VERIFIED,
        )
        claim = registry.record_claim(
            claim_id="strict_comparable",
            description="Candidate is a strict comparable",
            required_evidence=["target_genres", "genre_overlap"],
        )
        assert claim.status == ClaimStatus.GATED
        assert "target_genres" in claim.gated_by

    def test_dependency_propagation_contradicted(self):
        """EVID-7: Propagate CONTRADICTED status through dependency graph."""
        registry = EvidenceRegistry()
        # First add the dependency
        registry.add_dependency("derived_claim", "source_data")
        # Then record the source as contradicted
        registry.record_evidence(
            key="source_data",
            description="Source data contradicted",
            status=EvidenceStatus.CONTRADICTED,
        )
        # Now record the derived claim - it should be auto-contradicted
        registry.record_evidence(
            key="derived_claim",
            description="Derived from source data",
            status=EvidenceStatus.DERIVED,
        )
        # Source was contradicted, so derived should be contradicted too
        assert registry.items["derived_claim"].status == EvidenceStatus.CONTRADICTED

    def test_get_audit_summary(self):
        """EVID-8: Get audit summary with correct counts."""
        registry = EvidenceRegistry()
        registry.record_evidence(
            key="e1",
            description="Evidence 1",
            status=EvidenceStatus.VERIFIED,
        )
        registry.record_evidence(
            key="e2",
            description="Evidence 2",
            status=EvidenceStatus.NOT_VERIFIED,
        )
        registry.record_claim(
            claim_id="c1",
            description="Claim 1",
            required_evidence=["e1"],
        )
        registry.classify_candidate(
            candidate_id="comp_1",
            has_genre_overlap=True,
            has_entity_match=False,
            target_genres_verified=True,
        )
        summary = registry.get_audit_summary()
        assert summary["total_evidence"] == 2
        assert summary["verified_evidence"] == 1
        assert summary["not_verified_evidence"] == 1
        assert summary["total_claims"] == 1
        assert summary["supported_claims"] == 1
        assert summary["candidate_classifications"]["comp_1"] == "partial_match"

    def test_serialization_roundtrip(self):
        """EVID-9: Serialize and deserialize registry state."""
        registry = EvidenceRegistry()
        registry.record_evidence(
            key="target_genres",
            description="Target genres",
            status=EvidenceStatus.VERIFIED,
        )
        registry.record_claim(
            claim_id="strict_comparable",
            description="Strict comparable claim",
            required_evidence=["target_genres"],
        )
        registry.classify_candidate(
            candidate_id="comp_1",
            has_genre_overlap=True,
            has_entity_match=True,
            target_genres_verified=True,
        )

        # Serialize
        data = registry.to_dict()
        assert "items" in data
        assert "claims" in data
        assert "candidate_classifications" in data

        # Deserialize
        restored = EvidenceRegistry.from_dict(data)
        assert restored.items["target_genres"].status == EvidenceStatus.VERIFIED
        assert restored.claims["strict_comparable"].status == ClaimStatus.SUPPORTED
        assert restored.candidate_classifications["comp_1"] == CandidateClass.STRICT_COMPARABLE

    def test_target_absent_cannot_be_strict_comparable(self):
        """EVID-10: Target absent from ClickHouse prevents strict comparable classification."""
        registry = EvidenceRegistry()

        # Record that target is absent
        registry.record_evidence(
            key="target_genres",
            description="Target absent from ClickHouse — cannot verify genres",
            status=EvidenceStatus.NOT_VERIFIED,
        )

        # Even with genre overlap and entity match, cannot be strict comparable
        classification = registry.classify_candidate(
            candidate_id="comp_1",
            has_genre_overlap=True,
            has_entity_match=True,
            target_genres_verified=False,
        )
        assert classification == CandidateClass.UNVERIFIABLE

        # Claim should be gated
        claim = registry.record_claim(
            claim_id="strict_comparable",
            description="Candidate is a strict comparable",
            required_evidence=["target_genres", "genre_overlap"],
        )
        assert claim.status == ClaimStatus.GATED
        assert "target_genres" in claim.gated_by
