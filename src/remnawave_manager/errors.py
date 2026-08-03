class ManagerError(RuntimeError):
    """Ожидаемая ошибка, которую CLI показывает без traceback."""


class ValidationError(ManagerError):
    """Входные данные или найденная конфигурация небезопасны."""


class CommandError(ManagerError):
    """Внешняя команда завершилась с ошибкой."""


class TransactionError(ManagerError):
    """Транзакция обновления не дошла до контрольной точки."""

