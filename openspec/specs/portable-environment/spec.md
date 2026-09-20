# Переносимая операционная среда

## Purpose

Рабочие инструменты Olympus Max/Lite доступны на новом Mac Apple Silicon
после установки из проверенного архива и собственного входа владельца.

## Requirements

### Requirement: Portable operating environment
Поставка SHALL устанавливать инструменты на macOS Apple Silicon, создавая настройки
нового устройства и исключая личные материалы и учётные данные из архива.

#### Scenario: Clean installation
- **WHEN** the owner runs Install.command in an extracted release
- **THEN** the installer prepares dependencies and project-local tool configuration
- **AND** reports any remaining native login or operating system action explicitly

### Requirement: Repeatable and isolated setup
Установщик SHALL сохранять чужие настройки и изолировать Docker project Max,
тома, порт, рабочие каталоги и профиль авторизации модели.

#### Scenario: Repeated installation
- **WHEN** setup runs again in the same folder
- **THEN** it reuses matching settings and refuses to overwrite conflicting files

### Requirement: Reviewed distribution inventory
Архивы SHALL содержать только проверенный manifest и перечисленные исходники.

#### Scenario: Machine-local files appear
- **WHEN** credentials, caches, or local runtime files exist beside the source
- **THEN** they are excluded from the built release

### Requirement: Edition-specific readiness
Диагностика SHALL различать установленные инструменты, состояние служб
и авторизацию внешних сервисов.

#### Scenario: Lite installation
- **WHEN** Lite is installed
- **THEN** developer tools work without Docker or a model service
