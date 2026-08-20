class ManagerError(RuntimeError):
    """Ожидаемая ошибка, которую CLI показывает без traceback."""


class ValidationError(ManagerError):
    """Входные данные или найденная конфигурация небезопасны."""


class NodeSecretValidationError(ValidationError):
    """Node 3.3.2 отклонила содержимое SECRET_KEY."""


class CommandError(ManagerError):
    """Внешняя команда завершилась с ошибкой."""


class TransactionError(ManagerError):
    """Транзакция обновления не дошла до контрольной точки."""
