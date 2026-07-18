"""Public, stable operational error taxonomy."""


class OperationsError(RuntimeError):
    """Base class for expected operational failures."""


class ConfigurationError(OperationsError):
    pass


class MigrationDriftError(OperationsError):
    pass


class ClaimLost(OperationsError):
    pass


class OutboxBindingConflict(OperationsError):
    pass


class OutboxReplayRejected(OperationsError):
    pass


class ExperimentIdentityConflict(OperationsError):
    pass


class ExperimentRequestConflict(OperationsError):
    pass


class ActiveExperimentConflict(OperationsError):
    pass


class AdmissionClaimLost(OperationsError):
    pass


class MaintenanceFenceActive(OperationsError):
    pass


class AlertBindingConflict(OperationsError):
    pass


class AlertStateConflict(OperationsError):
    pass


class AlertDeliveryClaimLost(OperationsError):
    pass


class AlertTransportError(OperationsError):
    pass
