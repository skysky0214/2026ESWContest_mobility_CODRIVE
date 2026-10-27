> 경로 안내: 이 문서는 과거 작업 기록입니다. 파일은 현재 저장소의 `stm32/` 아래에 있으며, `E:\stm32` 대신 현재 체크아웃의 `stm32` 경로를 사용하세요. 최신 실행 순서는 [README.md](README.md)를 참고하세요.

# Complete source files

Current firmware: D14/PB9 TX, D15/PB8 RX. Full contents without omissions.

## CODRIVE_ECU/Core/Src/main.c

```c

/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    main.c
  * @brief   CODRIVE ECU - UART + Classic CAN VehicleState (500 kbit/s)
  ******************************************************************************
  */
/* USER CODE END Header */

#include "main.h"
#include <stdio.h>

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef struct
{
    float speed_kph;
    uint8_t brake_pressed;
    float steering_deg;
    uint32_t last_update_ms;
} VehicleState;
/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define CAN_ID_SPEED       0x100U
#define CAN_ID_BRAKE       0x101U
#define CAN_ID_STEERING    0x102U

#define CAN_TEST_PERIOD_MS 500U
/* USER CODE END PD */

/* Private variables ---------------------------------------------------------*/
FDCAN_HandleTypeDef hfdcan1;
UART_HandleTypeDef huart3;

/* USER CODE BEGIN PV */
static uint8_t rx_byte = 0;

volatile uint8_t last_rx_data = 0;
volatile uint32_t rx_count = 0;
volatile uint32_t uart_error_count = 0;
volatile uint32_t last_uart_error = 0;
volatile uint32_t uart_rearm_fail_count = 0;

volatile VehicleState vehicle_state = {0};

volatile uint32_t can_tx_count = 0;
volatile uint32_t can_rx_count = 0;
volatile uint32_t can_tx_error_count = 0;
volatile uint32_t can_rx_error_count = 0;
volatile uint32_t can_last_rx_id = 0;
volatile uint32_t can_last_hal_error = 0;
volatile uint32_t can_init_stage = 0;
volatile uint32_t can_valid_rx_count = 0;
volatile uint32_t can_invalid_frame_count = 0;
volatile uint32_t can_unknown_id_count = 0;
volatile uint32_t can_fifo_lost_count = 0;
volatile uint32_t can_bus_off_count = 0;
volatile uint32_t can_protocol_error_count = 0;
volatile uint32_t can_bus_off = 0;
volatile uint32_t can_last_protocol_error = 0;
volatile uint32_t can_kernel_clock_hz = 0;
volatile uint32_t can_nominal_bitrate = 0;
volatile uint32_t can_speed_rx_count = 0;
volatile uint32_t can_brake_rx_count = 0;
volatile uint32_t can_steering_rx_count = 0;
volatile uint32_t can_speed_last_ms = 0;
volatile uint32_t can_brake_last_ms = 0;
volatile uint32_t can_steering_last_ms = 0;
volatile uint32_t can_tx_error_counter = 0;
volatile uint32_t can_rx_error_counter = 0;
volatile uint32_t telemetry_tx_count = 0;
volatile uint32_t telemetry_skip_count = 0;
static char telemetry_buffer[320];

static uint32_t can_last_test_ms = 0;
static uint16_t test_speed_raw = 0;
static int16_t test_steering_raw = -300;
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART3_UART_Init(void);
static void MX_FDCAN1_Init(void);

/* USER CODE BEGIN PFP */
static void CAN_Setup(void);
static void CAN_Send(uint32_t id, const uint8_t data[8]);
static void CAN_ProcessRx(void);
static void CAN_SendTestData(void);
static void CAN_CheckStatus(void);
static void UART_SendVehicleTelemetry(void);
/* USER CODE END PFP */

/* USER CODE BEGIN 0 */

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART3)
    {
        return;
    }

    last_rx_data = rx_byte;
    rx_count++;

    switch (rx_byte)
    {
        case '1':
            HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_SET);
            break;

        case '0':
            HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_RESET);
            break;

        case 'T':
        case 't':
            HAL_GPIO_TogglePin(GPIOB, GPIO_PIN_0);
            break;

        default:
            break;
    }

    if (HAL_UART_Receive_IT(&huart3, &rx_byte, 1) != HAL_OK)
    {
        uart_rearm_fail_count++;
    }
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART3)
    {
        return;
    }

    uart_error_count++;
    last_uart_error = HAL_UART_GetError(huart);

    if (huart->RxState == HAL_UART_STATE_READY)
    {
        if (HAL_UART_Receive_IT(&huart3, &rx_byte, 1) != HAL_OK)
        {
            uart_rearm_fail_count++;
        }
    }
}

static void CAN_Setup(void)
{
    FDCAN_FilterTypeDef filter = {0};

    can_init_stage = 1;

    can_kernel_clock_hz = HAL_RCCEx_GetPeriphCLKFreq(RCC_PERIPHCLK_FDCAN);
    can_nominal_bitrate = can_kernel_clock_hz /
        (hfdcan1.Init.NominalPrescaler *
         (1U + hfdcan1.Init.NominalTimeSeg1 + hfdcan1.Init.NominalTimeSeg2));
    if (can_kernel_clock_hz != 128000000U || can_nominal_bitrate != 500000U)
    {
        Error_Handler();
    }

    filter.IdType = FDCAN_STANDARD_ID;
    filter.FilterIndex = 0;
    filter.FilterType = FDCAN_FILTER_MASK;
    filter.FilterConfig = FDCAN_FILTER_TO_RXFIFO0;
    filter.FilterID1 = 0;
    filter.FilterID2 = 0;

    if (HAL_FDCAN_ConfigFilter(&hfdcan1, &filter) != HAL_OK)
    {
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
        Error_Handler();
    }

    can_init_stage = 2;

    if (HAL_FDCAN_ConfigGlobalFilter(
            &hfdcan1,
            FDCAN_REJECT,
            FDCAN_REJECT,
            FDCAN_REJECT_REMOTE,
            FDCAN_REJECT_REMOTE) != HAL_OK)
    {
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
        Error_Handler();
    }

    can_init_stage = 3;

    if (HAL_FDCAN_Start(&hfdcan1) != HAL_OK)
    {
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
        Error_Handler();
    }

    can_init_stage = 4;
}

static void CAN_Send(uint32_t id, const uint8_t data[8])
{
    FDCAN_TxHeaderTypeDef tx_header = {0};

    tx_header.Identifier = id;
    tx_header.IdType = FDCAN_STANDARD_ID;
    tx_header.TxFrameType = FDCAN_DATA_FRAME;
    tx_header.DataLength = FDCAN_DLC_BYTES_8;
    tx_header.ErrorStateIndicator = FDCAN_ESI_ACTIVE;
    tx_header.BitRateSwitch = FDCAN_BRS_OFF;
    tx_header.FDFormat = FDCAN_CLASSIC_CAN;
    tx_header.TxEventFifoControl = FDCAN_NO_TX_EVENTS;
    tx_header.MessageMarker = 0;

    if (HAL_FDCAN_AddMessageToTxFifoQ(
            &hfdcan1,
            &tx_header,
            (uint8_t *)data) == HAL_OK)
    {
        can_tx_count++;
    }
    else
    {
        can_tx_error_count++;
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
    }
}

static void CAN_SendTestData(void)
{
    uint8_t speed_data[8] = {0};
    uint8_t brake_data[8] = {0};
    uint8_t steering_data[8] = {0};

    uint16_t steering_bits;

    test_speed_raw += 50U;

    if (test_speed_raw > 10000U)
    {
        test_speed_raw = 0;
    }

    test_steering_raw += 100;

    if (test_steering_raw > 300)
    {
        test_steering_raw = -300;
    }

    /* Speed: uint16 little-endian, 0.01 km/h */
    speed_data[0] = (uint8_t)(test_speed_raw & 0xFFU);
    speed_data[1] = (uint8_t)((test_speed_raw >> 8) & 0xFFU);

    /* Brake: 0 = OFF, 1 = ON */
    brake_data[0] =
        ((test_speed_raw / 50U) % 2U) ? 1U : 0U;

    /* Steering: signed int16 little-endian, 0.1 degree */
    steering_bits = (uint16_t)test_steering_raw;

    steering_data[0] = (uint8_t)(steering_bits & 0xFFU);
    steering_data[1] = (uint8_t)((steering_bits >> 8) & 0xFFU);

    CAN_Send(CAN_ID_SPEED, speed_data);
    CAN_Send(CAN_ID_BRAKE, brake_data);
    CAN_Send(CAN_ID_STEERING, steering_data);
}

static void CAN_ProcessRx(void)
{
    FDCAN_RxHeaderTypeDef rx_header = {0};
    uint8_t rx_data[8] = {0};

    while (HAL_FDCAN_GetRxFifoFillLevel(
               &hfdcan1, FDCAN_RX_FIFO0) > 0U)
    {
        if (HAL_FDCAN_GetRxMessage(
                &hfdcan1,
                FDCAN_RX_FIFO0,
                &rx_header,
                rx_data) != HAL_OK)
        {
            can_rx_error_count++;
            can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
            break;
        }

        can_rx_count++;
        can_last_rx_id = rx_header.Identifier;

        if (rx_header.IdType != FDCAN_STANDARD_ID ||
            rx_header.RxFrameType != FDCAN_DATA_FRAME ||
            rx_header.DataLength != FDCAN_DLC_BYTES_8 ||
            rx_header.FDFormat != FDCAN_CLASSIC_CAN)
        {
            can_invalid_frame_count++;
            continue;
        }

        switch (rx_header.Identifier)
        {
            case CAN_ID_SPEED:
            {
                uint16_t raw =
                    (uint16_t)rx_data[0] |
                    ((uint16_t)rx_data[1] << 8);

                vehicle_state.speed_kph = raw * 0.01f;
                vehicle_state.last_update_ms = HAL_GetTick();
                can_speed_last_ms = vehicle_state.last_update_ms;
                can_speed_rx_count++;
                can_valid_rx_count++;
                break;
            }

            case CAN_ID_BRAKE:
            {
                if (rx_data[0] > 1U)
                {
                    can_invalid_frame_count++;
                    break;
                }
                vehicle_state.brake_pressed =
                    (rx_data[0] != 0U) ? 1U : 0U;

                vehicle_state.last_update_ms = HAL_GetTick();
                can_brake_last_ms = vehicle_state.last_update_ms;
                can_brake_rx_count++;
                can_valid_rx_count++;
                break;
            }

            case CAN_ID_STEERING:
            {
                uint16_t bits =
                    (uint16_t)rx_data[0] |
                    ((uint16_t)rx_data[1] << 8);

                int32_t signed_raw =
                    (bits & 0x8000U)
                    ? ((int32_t)bits - 65536)
                    : (int32_t)bits;

                vehicle_state.steering_deg =
                    signed_raw * 0.1f;

                vehicle_state.last_update_ms = HAL_GetTick();
                can_steering_last_ms = vehicle_state.last_update_ms;
                can_steering_rx_count++;
                can_valid_rx_count++;
                break;
            }

            default:
                can_unknown_id_count++;
                break;
        }
    }
}

/* Poll status as well as FIFO; no second receive path or new UART protocol. */
static void CAN_CheckStatus(void)
{
    static uint32_t last_check_ms = 0;
    FDCAN_ProtocolStatusTypeDef status = {0};
    FDCAN_ErrorCountersTypeDef counters = {0};

    if (__HAL_FDCAN_GET_FLAG(&hfdcan1, FDCAN_FLAG_RX_FIFO0_MESSAGE_LOST))
    {
        __HAL_FDCAN_CLEAR_FLAG(&hfdcan1, FDCAN_FLAG_RX_FIFO0_MESSAGE_LOST);
        can_fifo_lost_count++; /* Latched events, not an exact lost-frame count. */
    }
    if ((HAL_GetTick() - last_check_ms) < 100U)
    {
        return;
    }
    last_check_ms = HAL_GetTick();
    if (HAL_FDCAN_GetProtocolStatus(&hfdcan1, &status) == HAL_OK)
    {
        if (status.BusOff && !can_bus_off)
        {
            can_bus_off_count++;
        }
        can_bus_off = status.BusOff;
        if (status.LastErrorCode > 0U && status.LastErrorCode < 7U)
        {
            can_last_protocol_error = status.LastErrorCode;
            can_protocol_error_count++; /* Error observations between status reads. */
        }
    }
    else
    {
        can_rx_error_count++;
        can_last_hal_error = HAL_FDCAN_GetError(&hfdcan1);
    }
    if (HAL_FDCAN_GetErrorCounters(&hfdcan1, &counters) == HAL_OK)
    {
        can_tx_error_counter = counters.TxErrorCnt;
        can_rx_error_counter = counters.RxErrorCnt;
    }
}

/* Nonblocking TX; UART RX interrupt and existing LED commands stay active.
 * Static buffer is only overwritten once the previous TX has completed. */
static void UART_SendVehicleTelemetry(void)
{
    static uint32_t last_ms = 0;
    uint32_t now = HAL_GetTick();
    int length;
    int32_t steering_tenths;
    if ((now - last_ms) < 200U)
    {
        return;
    }
    last_ms = now;
    if (huart3.gState != HAL_UART_STATE_READY)
    {
        telemetry_skip_count++;
        return;
    }
    steering_tenths = (int32_t)(vehicle_state.steering_deg * 10.0f +
                         ((vehicle_state.steering_deg >= 0.0f) ? 0.5f : -0.5f));
    length = snprintf(telemetry_buffer, sizeof(telemetry_buffer),
        "ECU,ms=%lu,speed_raw=%lu,brake=%u,steer_raw=%ld,rx=%lu,valid=%lu,"
        "speed_n=%lu,brake_n=%lu,steer_n=%lu,speed_ms=%lu,brake_ms=%lu,steer_ms=%lu,"
        "err=%lu,invalid=%lu,lost=%lu,bo=%lu,lec=%lu\r\n",
        (unsigned long)now,
        (unsigned long)(vehicle_state.speed_kph * 100.0f + 0.5f),
        (unsigned int)vehicle_state.brake_pressed, (long)steering_tenths,
        (unsigned long)can_rx_count, (unsigned long)can_valid_rx_count,
        (unsigned long)can_speed_rx_count, (unsigned long)can_brake_rx_count,
        (unsigned long)can_steering_rx_count,
        (unsigned long)can_speed_last_ms, (unsigned long)can_brake_last_ms,
        (unsigned long)can_steering_last_ms,
        (unsigned long)can_rx_error_count, (unsigned long)can_invalid_frame_count,
        (unsigned long)can_fifo_lost_count, (unsigned long)can_bus_off,
        (unsigned long)can_last_protocol_error);
    if (length > 0 && (size_t)length < sizeof(telemetry_buffer) &&
        HAL_UART_Transmit_IT(&huart3, (uint8_t *)telemetry_buffer, (uint16_t)length) == HAL_OK)
    {
        telemetry_tx_count++;
    }
    else
    {
        telemetry_skip_count++;
    }
}

/* USER CODE END 0 */

int main(void)
{
    HAL_Init();
    SystemClock_Config();

    MX_GPIO_Init();
    MX_USART3_UART_Init();
    MX_FDCAN1_Init();

    /* USER CODE BEGIN 2 */
    if (HAL_UART_Receive_IT(&huart3, &rx_byte, 1) != HAL_OK)
    {
        Error_Handler();
    }

    CAN_Setup();
    can_last_test_ms = HAL_GetTick();
    /* USER CODE END 2 */

    /* Infinite loop */
    /* USER CODE BEGIN WHILE */
    while (1)
    {
        CAN_ProcessRx();

        /* Never send loopback dummy values onto the real bus. */
        if (hfdcan1.Init.Mode == FDCAN_MODE_INTERNAL_LOOPBACK &&
            (HAL_GetTick() - can_last_test_ms) >= CAN_TEST_PERIOD_MS)
        {
            can_last_test_ms = HAL_GetTick();
            CAN_SendTestData();
        }

        CAN_ProcessRx();
        CAN_CheckStatus();
        UART_SendVehicleTelemetry();
    }
    /* USER CODE END WHILE */
}

void SystemClock_Config(void)
{
    RCC_OscInitTypeDef RCC_OscInitStruct = {0};
    RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

    HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY);

    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE3);

    while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY))
    {
    }

    RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
    RCC_OscInitStruct.HSIState = RCC_HSI_DIV1;
    RCC_OscInitStruct.HSICalibrationValue = 64;
    RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSI;
    RCC_OscInitStruct.PLL.PLLM = 32;
    RCC_OscInitStruct.PLL.PLLN = 128;
    RCC_OscInitStruct.PLL.PLLP = 2;
    RCC_OscInitStruct.PLL.PLLQ = 2;
    RCC_OscInitStruct.PLL.PLLR = 2;
    RCC_OscInitStruct.PLL.PLLRGE = RCC_PLL1VCIRANGE_1;
    RCC_OscInitStruct.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
    RCC_OscInitStruct.PLL.PLLFRACN = 0;

    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
        Error_Handler();
    }

    RCC_ClkInitStruct.ClockType =
        RCC_CLOCKTYPE_HCLK |
        RCC_CLOCKTYPE_SYSCLK |
        RCC_CLOCKTYPE_PCLK1 |
        RCC_CLOCKTYPE_PCLK2 |
        RCC_CLOCKTYPE_D3PCLK1 |
        RCC_CLOCKTYPE_D1PCLK1;

    RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_HSI;
    RCC_ClkInitStruct.SYSCLKDivider = RCC_SYSCLK_DIV1;
    RCC_ClkInitStruct.AHBCLKDivider = RCC_HCLK_DIV1;
    RCC_ClkInitStruct.APB3CLKDivider = RCC_APB3_DIV1;
    RCC_ClkInitStruct.APB1CLKDivider = RCC_APB1_DIV1;
    RCC_ClkInitStruct.APB2CLKDivider = RCC_APB2_DIV1;
    RCC_ClkInitStruct.APB4CLKDivider = RCC_APB4_DIV1;

    if (HAL_RCC_ClockConfig(
            &RCC_ClkInitStruct,
            FLASH_LATENCY_1) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_FDCAN1_Init(void)
{
    hfdcan1.Instance = FDCAN1;
    hfdcan1.Init.FrameFormat = FDCAN_FRAME_CLASSIC;
    hfdcan1.Init.Mode = FDCAN_MODE_NORMAL;
    hfdcan1.Init.AutoRetransmission = DISABLE;
    hfdcan1.Init.TransmitPause = DISABLE;
    hfdcan1.Init.ProtocolException = DISABLE;

    hfdcan1.Init.NominalPrescaler = 4;
    hfdcan1.Init.NominalSyncJumpWidth = 1;
    hfdcan1.Init.NominalTimeSeg1 = 56;
    hfdcan1.Init.NominalTimeSeg2 = 7;

    hfdcan1.Init.DataPrescaler = 1;
    hfdcan1.Init.DataSyncJumpWidth = 1;
    hfdcan1.Init.DataTimeSeg1 = 1;
    hfdcan1.Init.DataTimeSeg2 = 1;

    hfdcan1.Init.MessageRAMOffset = 0;
    hfdcan1.Init.StdFiltersNbr = 1;
    hfdcan1.Init.ExtFiltersNbr = 0;

    hfdcan1.Init.RxFifo0ElmtsNbr = 3;
    hfdcan1.Init.RxFifo0ElmtSize = FDCAN_DATA_BYTES_8;

    hfdcan1.Init.RxFifo1ElmtsNbr = 0;
    hfdcan1.Init.RxFifo1ElmtSize = FDCAN_DATA_BYTES_8;

    hfdcan1.Init.RxBuffersNbr = 0;
    hfdcan1.Init.RxBufferSize = FDCAN_DATA_BYTES_8;

    hfdcan1.Init.TxEventsNbr = 0;
    hfdcan1.Init.TxBuffersNbr = 0;
    hfdcan1.Init.TxFifoQueueElmtsNbr = 3;
    hfdcan1.Init.TxFifoQueueMode = FDCAN_TX_FIFO_OPERATION;
    hfdcan1.Init.TxElmtSize = FDCAN_DATA_BYTES_8;

    if (HAL_FDCAN_Init(&hfdcan1) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_USART3_UART_Init(void)
{
    huart3.Instance = USART3;
    huart3.Init.BaudRate = 115200;
    huart3.Init.WordLength = UART_WORDLENGTH_8B;
    huart3.Init.StopBits = UART_STOPBITS_1;
    huart3.Init.Parity = UART_PARITY_NONE;
    huart3.Init.Mode = UART_MODE_TX_RX;
    huart3.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart3.Init.OverSampling = UART_OVERSAMPLING_16;
    huart3.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
    huart3.Init.ClockPrescaler = UART_PRESCALER_DIV1;
    huart3.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;

    if (HAL_UART_Init(&huart3) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_UARTEx_SetTxFifoThreshold(
            &huart3,
            UART_TXFIFO_THRESHOLD_1_8) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_UARTEx_SetRxFifoThreshold(
            &huart3,
            UART_RXFIFO_THRESHOLD_1_8) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_UARTEx_DisableFifoMode(&huart3) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_GPIO_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};

    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();

    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_0, GPIO_PIN_RESET);

    GPIO_InitStruct.Pin = GPIO_PIN_0;
    GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;

    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);
}

void Error_Handler(void)
{
    __disable_irq();

    while (1)
    {
    }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{
    (void)file;
    (void)line;
}
#endif
```

## CODRIVE_ECU/Core/Src/stm32h7xx_hal_msp.c

```c
/* USER CODE BEGIN Header */
/**
 ******************************************************************************
 * @file    stm32h7xx_hal_msp.c
 * @brief   MSP Initialization for CODRIVE ECU
 ******************************************************************************
 */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
/* USER CODE BEGIN Includes */
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN TD */

/* USER CODE END TD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN Define */

/* USER CODE END Define */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN Macro */

/* USER CODE END Macro */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* External functions --------------------------------------------------------*/
/* USER CODE BEGIN ExternalFunctions */

/* USER CODE END ExternalFunctions */

/* USER CODE BEGIN 0 */
/* USER CODE END 0 */
/**
  * Initializes the Global MSP.
  */
void HAL_MspInit(void)
{

  /* USER CODE BEGIN MspInit 0 */

  /* USER CODE END MspInit 0 */

  __HAL_RCC_SYSCFG_CLK_ENABLE();

  /* System interrupt init*/

  /* USER CODE BEGIN MspInit 1 */

  /* USER CODE END MspInit 1 */
}

/**
  * @brief FDCAN MSP Initialization
  * This function configures the hardware resources used in this example
  * @param hfdcan: FDCAN handle pointer
  * @retval None
  */
void HAL_FDCAN_MspInit(FDCAN_HandleTypeDef* hfdcan)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInitStruct = {0};
  if(hfdcan->Instance==FDCAN1)
  {
    /* USER CODE BEGIN FDCAN1_MspInit 0 */

    /* USER CODE END FDCAN1_MspInit 0 */

  /** Initializes the peripherals clock
  */
    PeriphClkInitStruct.PeriphClockSelection = RCC_PERIPHCLK_FDCAN;
    PeriphClkInitStruct.FdcanClockSelection = RCC_FDCANCLKSOURCE_PLL;
    if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInitStruct) != HAL_OK)
    {
      Error_Handler();
    }

    /* Peripheral clock enable */
    __HAL_RCC_FDCAN_CLK_ENABLE();

    __HAL_RCC_GPIOB_CLK_ENABLE();
    /**FDCAN1 GPIO Configuration
    PB8 (D15) ------> FDCAN1_RX
    PB9 (D14) ------> FDCAN1_TX
    */
    GPIO_InitStruct.Pin = GPIO_PIN_8|GPIO_PIN_9;
    GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
    GPIO_InitStruct.Alternate = GPIO_AF9_FDCAN1;
    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

    /* FDCAN1 interrupt Init */
    HAL_NVIC_SetPriority(FDCAN1_IT0_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(FDCAN1_IT0_IRQn);
    /* USER CODE BEGIN FDCAN1_MspInit 1 */

    /* USER CODE END FDCAN1_MspInit 1 */

  }

}

/**
  * @brief FDCAN MSP De-Initialization
  * This function freeze the hardware resources used in this example
  * @param hfdcan: FDCAN handle pointer
  * @retval None
  */
void HAL_FDCAN_MspDeInit(FDCAN_HandleTypeDef* hfdcan)
{
  if(hfdcan->Instance==FDCAN1)
  {
    /* USER CODE BEGIN FDCAN1_MspDeInit 0 */

    /* USER CODE END FDCAN1_MspDeInit 0 */
    /* Peripheral clock disable */
    __HAL_RCC_FDCAN_CLK_DISABLE();

    /**FDCAN1 GPIO Configuration
    PB8 (D15) ------> FDCAN1_RX
    PB9 (D14) ------> FDCAN1_TX
    */
    HAL_GPIO_DeInit(GPIOB, GPIO_PIN_8|GPIO_PIN_9);

    /* FDCAN1 interrupt DeInit */
    HAL_NVIC_DisableIRQ(FDCAN1_IT0_IRQn);
    /* USER CODE BEGIN FDCAN1_MspDeInit 1 */

    /* USER CODE END FDCAN1_MspDeInit 1 */
  }

}

/**
  * @brief UART MSP Initialization
  * This function configures the hardware resources used in this example
  * @param huart: UART handle pointer
  * @retval None
  */
void HAL_UART_MspInit(UART_HandleTypeDef* huart)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInitStruct = {0};
  if(huart->Instance==USART3)
  {
    /* USER CODE BEGIN USART3_MspInit 0 */

    /* USER CODE END USART3_MspInit 0 */

  /** Initializes the peripherals clock
  */
    PeriphClkInitStruct.PeriphClockSelection = RCC_PERIPHCLK_USART3;
    PeriphClkInitStruct.Usart234578ClockSelection = RCC_USART234578CLKSOURCE_D2PCLK1;
    if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInitStruct) != HAL_OK)
    {
      Error_Handler();
    }

    /* Peripheral clock enable */
    __HAL_RCC_USART3_CLK_ENABLE();

    __HAL_RCC_GPIOD_CLK_ENABLE();
    /**USART3 GPIO Configuration
    PD8     ------> USART3_TX
    PD9     ------> USART3_RX
    */
    GPIO_InitStruct.Pin = GPIO_PIN_8|GPIO_PIN_9;
    GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
    GPIO_InitStruct.Alternate = GPIO_AF7_USART3;
    HAL_GPIO_Init(GPIOD, &GPIO_InitStruct);

    /* USART3 interrupt Init */
    HAL_NVIC_SetPriority(USART3_IRQn, 0, 0);
    HAL_NVIC_EnableIRQ(USART3_IRQn);
    /* USER CODE BEGIN USART3_MspInit 1 */

    /* USER CODE END USART3_MspInit 1 */

  }

}

/**
  * @brief UART MSP De-Initialization
  * This function freeze the hardware resources used in this example
  * @param huart: UART handle pointer
  * @retval None
  */
void HAL_UART_MspDeInit(UART_HandleTypeDef* huart)
{
  if(huart->Instance==USART3)
  {
    /* USER CODE BEGIN USART3_MspDeInit 0 */

    /* USER CODE END USART3_MspDeInit 0 */
    /* Peripheral clock disable */
    __HAL_RCC_USART3_CLK_DISABLE();

    /**USART3 GPIO Configuration
    PD8     ------> USART3_TX
    PD9     ------> USART3_RX
    */
    HAL_GPIO_DeInit(GPIOD, GPIO_PIN_8|GPIO_PIN_9);

    /* USART3 interrupt DeInit */
    HAL_NVIC_DisableIRQ(USART3_IRQn);
    /* USER CODE BEGIN USART3_MspDeInit 1 */

    /* USER CODE END USART3_MspDeInit 1 */
  }

}

/* USER CODE BEGIN 1 */
/* USER CODE END 1 */
```

## CODRIVE_ECU/CODRIVE_ECU.ioc

```ini
#MicroXplorer Configuration settings - do not modify
BSP_IP_NAME=NUCLEO-H723ZG
CAD.formats=
CAD.pinconfig=
CAD.provider=
FDCAN1.CalculateBaudRateNominal=500000
FDCAN1.CalculateTimeBitNominal=2000
FDCAN1.CalculateTimeQuantumNominal=31.25
FDCAN1.IPParameters=CalculateTimeQuantumNominal,CalculateTimeBitNominal,CalculateBaudRateNominal,Mode,StdFiltersNbr,RxFifo0ElmtsNbr,NominalPrescaler,NominalTimeSeg1,NominalTimeSeg2,FrameFormat,RxFifo0ElmtSize,TxFifoQueueElmtsNbr,TxFifoQueueMode,TxElmtSize
FDCAN1.FrameFormat=FDCAN_FRAME_CLASSIC
FDCAN1.RxFifo0ElmtSize=FDCAN_DATA_BYTES_8
FDCAN1.TxFifoQueueElmtsNbr=3
FDCAN1.TxFifoQueueMode=FDCAN_TX_FIFO_OPERATION
FDCAN1.TxElmtSize=FDCAN_DATA_BYTES_8
FDCAN1.Mode=FDCAN_MODE_NORMAL
FDCAN1.NominalPrescaler=4
FDCAN1.NominalTimeSeg1=56
FDCAN1.NominalTimeSeg2=7
FDCAN1.RxFifo0ElmtsNbr=3
FDCAN1.StdFiltersNbr=1
File.Version=6
GPIO.groupedBy=
KeepUserPlacement=false
MMTAppRegionsCount=0
MMTConfigApplied=false
MMTSectionSuffix=
Mcu.CPN=STM32H723ZGT6
Mcu.Family=STM32H7
Mcu.IP0=CORTEX_M7
Mcu.IP1=FDCAN1
Mcu.IP2=MEMORYMAP
Mcu.IP3=NVIC
Mcu.IP4=RCC
Mcu.IP5=SYS
Mcu.IP6=USART3
Mcu.IP7=NUCLEO-H723ZG
Mcu.IPNb=8
Mcu.Name=STM32H723ZGTx
Mcu.Package=LQFP144
Mcu.Pin0=PB0
Mcu.Pin1=PD8
Mcu.Pin2=PD9
Mcu.Pin3=PB8
Mcu.Pin4=PB9
Mcu.Pin5=VP_SYS_VS_Systick
Mcu.Pin6=VP_MEMORYMAP_VS_MEMORYMAP
Mcu.PinsNb=7
Mcu.ThirdPartyNb=0
Mcu.UserConstants=
Mcu.UserName=STM32H723ZGTx
MxCube.Version=6.18.1
MxDb.Version=DB.6.0.181
NVIC.BusFault_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.DebugMonitor_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.FDCAN1_IT0_IRQn=true\:0\:0\:false\:false\:true\:true\:true\:true
NVIC.ForceEnableDMAVector=true
NVIC.HardFault_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.MemoryManagement_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.NonMaskableInt_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.PendSV_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.PriorityGroup=NVIC_PRIORITYGROUP_4
NVIC.SVCall_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
NVIC.SysTick_IRQn=true\:15\:0\:false\:false\:true\:false\:true\:false
NVIC.USART3_IRQn=true\:0\:0\:false\:false\:true\:true\:true\:true
NVIC.UsageFault_IRQn=true\:0\:0\:false\:false\:true\:false\:false\:false
PB8.Mode=FDCAN_Activate
PB8.Signal=FDCAN1_RX
PB9.Mode=FDCAN_Activate
PB9.Signal=FDCAN1_TX
PB0.Locked=true
PB0.Signal=GPIO_Output
PD8.Locked=true
PD8.Mode=Asynchronous
PD8.Signal=USART3_TX
PD9.Locked=true
PD9.Mode=Asynchronous
PD9.Signal=USART3_RX
PinOutPanel.RotationAngle=0
ProjectManager.AskForMigrate=true
ProjectManager.BackupPrevious=false
ProjectManager.CompilerLinker=GCC
ProjectManager.CompilerOptimize=6
ProjectManager.ComputerToolchain=false
ProjectManager.CoupleFile=false
ProjectManager.CustomerFirmwarePackage=
ProjectManager.DefaultFWLocation=true
ProjectManager.DeletePrevious=true
ProjectManager.DeviceId=STM32H723ZGTx
ProjectManager.FirmwarePackage=STM32Cube FW_H7 V1.13.0
ProjectManager.FreePins=false
ProjectManager.FreePinsContext=
ProjectManager.HalAssertFull=false
ProjectManager.HeapSize=0x200
ProjectManager.KeepUserCode=true
ProjectManager.LastFirmware=true
ProjectManager.LibraryCopy=1
ProjectManager.MainLocation=Core/Src
ProjectManager.NoMain=false
ProjectManager.PreviousToolchain=STM32CubeIDE
ProjectManager.ProjectBuild=false
ProjectManager.ProjectFileName=CODRIVE_ECU.ioc
ProjectManager.ProjectName=CODRIVE_ECU
ProjectManager.ProjectStructure=
ProjectManager.RegisterCallBack=
ProjectManager.StackSize=0x400
ProjectManager.TargetToolchain=STM32CubeIDE
ProjectManager.ToolChainLocation=
ProjectManager.UAScriptAfterPath=
ProjectManager.UAScriptBeforePath=
ProjectManager.UnderRoot=true
ProjectManager.functionlistsort=1-SystemClock_Config-RCC-false-HAL-false,2-MX_GPIO_Init-GPIO-false-HAL-true,3-MX_USART3_UART_Init-USART3-false-HAL-true,0-MX_CORTEX_M7_Init-CORTEX_M7-false-HAL-true
RCC.ADCFreq_Value=128000000
RCC.AHB12Freq_Value=64000000
RCC.AHB4Freq_Value=64000000
RCC.APB1Freq_Value=64000000
RCC.APB2Freq_Value=64000000
RCC.APB3Freq_Value=64000000
RCC.APB4Freq_Value=64000000
RCC.AXIClockFreq_Value=64000000
RCC.CECFreq_Value=32000
RCC.CKPERFreq_Value=64000000
RCC.CortexFreq_Value=64000000
RCC.CpuClockFreq_Value=64000000
RCC.D1CPREFreq_Value=64000000
RCC.DFSDMACLkFreq_Value=128000000
RCC.DFSDMFreq_Value=64000000
RCC.DIVP1Freq_Value=128000000
RCC.DIVP2Freq_Value=128000000
RCC.DIVP3Freq_Value=128000000
RCC.DIVQ1Freq_Value=128000000
RCC.DIVQ2Freq_Value=128000000
RCC.DIVQ3Freq_Value=128000000
RCC.DIVR1Freq_Value=128000000
RCC.DIVR2Freq_Value=128000000
RCC.DIVR3Freq_Value=128000000
RCC.FDCANFreq_Value=128000000
RCC.FMCFreq_Value=64000000
RCC.FamilyName=M
RCC.HCLK3ClockFreq_Value=64000000
RCC.HCLKFreq_Value=64000000
RCC.I2C123Freq_Value=64000000
RCC.I2C4Freq_Value=64000000
RCC.IPParameters=PLLN,ADCFreq_Value,AHB12Freq_Value,AHB4Freq_Value,APB1Freq_Value,APB2Freq_Value,APB3Freq_Value,APB4Freq_Value,AXIClockFreq_Value,CECFreq_Value,CKPERFreq_Value,CortexFreq_Value,CpuClockFreq_Value,D1CPREFreq_Value,DFSDMACLkFreq_Value,DFSDMFreq_Value,DIVP1Freq_Value,DIVP2Freq_Value,DIVP3Freq_Value,DIVQ1Freq_Value,DIVQ2Freq_Value,DIVQ3Freq_Value,DIVR1Freq_Value,DIVR2Freq_Value,DIVR3Freq_Value,FDCANFreq_Value,FMCFreq_Value,FamilyName,HCLK3ClockFreq_Value,HCLKFreq_Value,I2C123Freq_Value,I2C4Freq_Value,LPTIM1Freq_Value,LPTIM2Freq_Value,LPTIM345Freq_Value,LPUART1Freq_Value,LTDCFreq_Value,MCO1PinFreq_Value,MCO2PinFreq_Value,QSPIFreq_Value,RNGFreq_Value,RTCFreq_Value,SAI1Freq_Value,SAI4AFreq_Value,SAI4BFreq_Value,SDMMCFreq_Value,SPDIFRXFreq_Value,SPI123Freq_Value,SPI45Freq_Value,SPI6Freq_Value,SWPMI1Freq_Value,SYSCLKFreq_VALUE,Tim1OutputFreq_Value,Tim2OutputFreq_Value,TraceFreq_Value,USART16Freq_Value,USART234578Freq_Value,USBFreq_Value,VCO1OutputFreq_Value,VCO2OutputFreq_Value,VCO3OutputFreq_Value,VCOInput1Freq_Value,VCOInput2Freq_Value,VCOInput3Freq_Value
RCC.PLLN=128
RCC.LPTIM1Freq_Value=64000000
RCC.LPTIM2Freq_Value=64000000
RCC.LPTIM345Freq_Value=64000000
RCC.LPUART1Freq_Value=64000000
RCC.LTDCFreq_Value=128000000
RCC.MCO1PinFreq_Value=64000000
RCC.MCO2PinFreq_Value=64000000
RCC.QSPIFreq_Value=64000000
RCC.RNGFreq_Value=48000000
RCC.RTCFreq_Value=32000
RCC.SAI1Freq_Value=128000000
RCC.SAI4AFreq_Value=128000000
RCC.SAI4BFreq_Value=128000000
RCC.SDMMCFreq_Value=128000000
RCC.SPDIFRXFreq_Value=128000000
RCC.SPI123Freq_Value=128000000
RCC.SPI45Freq_Value=64000000
RCC.SPI6Freq_Value=64000000
RCC.SWPMI1Freq_Value=64000000
RCC.SYSCLKFreq_VALUE=64000000
RCC.Tim1OutputFreq_Value=64000000
RCC.Tim2OutputFreq_Value=64000000
RCC.TraceFreq_Value=64000000
RCC.USART16Freq_Value=64000000
RCC.USART234578Freq_Value=64000000
RCC.USBFreq_Value=128000000
RCC.VCO1OutputFreq_Value=256000000
RCC.VCO2OutputFreq_Value=256000000
RCC.VCO3OutputFreq_Value=256000000
RCC.VCOInput1Freq_Value=2000000
RCC.VCOInput2Freq_Value=2000000
RCC.VCOInput3Freq_Value=2000000
USART3.IPParameters=VirtualMode-Asynchronous
USART3.VirtualMode-Asynchronous=VM_ASYNC
VP_MEMORYMAP_VS_MEMORYMAP.Mode=CurAppReg
VP_MEMORYMAP_VS_MEMORYMAP.Signal=MEMORYMAP_VS_MEMORYMAP
VP_SYS_VS_Systick.Mode=SysTick
VP_SYS_VS_Systick.Signal=SYS_VS_Systick
board=NUCLEO-H723ZG
```

## g29_test.py

```python
"""G29 raw monitor and measured calibration. Run with Python 3.12.

py -3.12 E:\\stm32\\g29_test.py --calibrate
Calibration: focus the window; hold each requested position and press Enter.
The axis numbers come from the measured changes, never from a default mapping.
"""

import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import statistics
import time

CONFIG = Path(__file__).with_name("g29_axes.json")


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite calibration/input value")
    return value


def clamp(value, low, high):
    return max(low, min(high, value))


def validate_config(config, guid=None, num_axes=None):
    if config.get("version") != 1:
        raise ValueError("Unsupported calibration version; run --calibrate")
    if guid is not None and config.get("guid") != guid:
        raise ValueError("G29 GUID changed (driver/backend/device); recalibrate")
    axes = []
    for name in ("steering", "throttle", "brake"):
        item = config[name]
        axis = item["axis"]
        if type(axis) is not int or axis < 0 or (num_axes is not None and axis >= num_axes):
            raise ValueError(f"Invalid {name} axis index")
        axes.append(axis)
        if name == "steering":
            left, center, right = (finite(item[k]) for k in ("left", "center", "right"))
            if (left - center) * (right - center) >= 0 or min(abs(left-center), abs(right-center)) < 0.2:
                raise ValueError("Steering endpoints must straddle center with sufficient travel")
        elif abs(finite(item["pressed"]) - finite(item["rest"])) < 0.2:
            raise ValueError(f"Insufficient {name} travel")
    if len(set(axes)) != 3:
        raise ValueError("Combined/shared pedal axes are unsupported; enable separate pedals and recalibrate")
    return config


def load_config(path=CONFIG, guid=None, num_axes=None):
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8")), guid, num_axes)


def normalize(axes, config):
    s = config["steering"]
    raw = finite(axes[s["axis"]])
    delta = raw - s["center"]
    if delta * (s["right"] - s["center"]) >= 0:
        steering = 450 * clamp(delta / (s["right"] - s["center"]), 0, 1)
    else:
        steering = -450 * clamp(delta / (s["left"] - s["center"]), 0, 1)
    result = {"steering_deg": steering}
    for name in ("throttle", "brake"):
        p = config[name]
        result[name + "_pct"] = 100 * clamp(
            (finite(axes[p["axis"]]) - p["rest"]) / (p["pressed"] - p["rest"]), 0, 1)
    return result


class Wheel:
    def __init__(self, device_index=None):
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("SDL_JOYSTICK_HIDAPI", "0")
        os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")
        import pygame
        self.pg = pygame
        pygame.display.init()
        pygame.font.init()
        pygame.joystick.init()
        self.screen = pygame.display.set_mode((1000, 420))
        pygame.display.set_caption("CODRIVE G29 - calibration / input monitor")
        self.font = pygame.font.Font(None, 25)
        devices = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        print("Devices:", [(i, d.get_name(), d.get_guid()) for i, d in enumerate(devices)], flush=True)
        if device_index is not None:
            if not 0 <= device_index < len(devices):
                raise ValueError("Device index out of range")
            candidates = [devices[device_index]]
        else:
            candidates = [d for d in devices if "G29" in d.get_name().upper()]
        if len(candidates) != 1 or "G29" not in candidates[0].get_name().upper():
            raise ValueError("Connect one G29, or select its index using --device-index")
        self.joy = candidates[0]
        self.guid = self.joy.get_guid()
        self.count = self.joy.get_numaxes()
        self.enter = False

    def read(self):
        pg = self.pg
        self.enter = False
        for event in pg.event.get():
            if event.type == pg.QUIT:
                raise KeyboardInterrupt
            if event.type == pg.JOYDEVICEREMOVED and event.instance_id == self.joy.get_instance_id():
                raise RuntimeError("G29 disconnected. Stop; reconnect and restart (no stale values sent).")
            if event.type == pg.KEYDOWN:
                if event.key == pg.K_ESCAPE:
                    raise KeyboardInterrupt
                if event.key in (pg.K_RETURN, pg.K_KP_ENTER):
                    self.enter = True
        axes = [self.joy.get_axis(i) for i in range(self.count)]
        buttons = [i for i in range(self.joy.get_numbuttons()) if self.joy.get_button(i)]
        hats = [self.joy.get_hat(i) for i in range(self.joy.get_numhats())]
        return axes, buttons, hats

    def draw(self, lines):
        self.screen.fill((24, 28, 36))
        for row, line in enumerate(lines):
            self.screen.blit(self.font.render(str(line), True, (235, 240, 245)), (16, 16 + row * 35))
        self.pg.display.flip()

    def close(self):
        self.pg.quit()


def calibrate(wheel, path, overrides):
    steps = [
        ("center", "Wheel CENTER, release BOTH pedals. Hold steady, press ENTER."),
        ("left", "Wheel fully LEFT (-450 deg), pedals released. Hold, press ENTER."),
        ("right", "Wheel fully RIGHT (+450 deg), pedals released. Hold, press ENTER."),
        ("throttle_rest", "Wheel CENTER, release BOTH pedals. Hold, press ENTER."),
        ("throttle_full", "Press ONLY accelerator fully. Hold, press ENTER."),
        ("brake_rest", "Release BOTH pedals. Hold, press ENTER."),
        ("brake_full", "Press ONLY brake fully. Hold, press ENTER."),
    ]
    samples = {}
    for key, prompt in steps:
        print(prompt, flush=True)
        history = deque(maxlen=15)
        while True:
            axes, _, _ = wheel.read()
            history.append(axes)
            wheel.draw(["CALIBRATION - click window; ESC cancels", prompt,
                        "Raw: " + str([round(a, 3) for a in axes]),
                        "Keep position stable for at least 0.3 seconds before ENTER."])
            if wheel.enter and len(history) == history.maxlen:
                if any(max(col)-min(col) > 0.04 for col in zip(*history)):
                    print("Inputs unstable; hold steady and press ENTER again.", flush=True)
                else:
                    samples[key] = [statistics.median(col) for col in zip(*history)]
                    break
            time.sleep(0.02)

    def choose(name, low, high):
        changes = [abs(a-b) for a, b in zip(samples[low], samples[high])]
        axis = overrides.get(name)
        if axis is None:
            ranked = sorted(range(wheel.count), key=lambda i: changes[i], reverse=True)
            axis = ranked[0]
            if len(ranked) > 1 and changes[ranked[1]] > changes[axis] * 0.5:
                raise ValueError(f"Ambiguous {name} axis; repeat, or specify --{name}-axis INDEX")
        if not 0 <= axis < wheel.count or changes[axis] < 0.2:
            raise ValueError(f"No sufficient movement for {name}; retry calibration")
        return axis

    s = choose("steering", "left", "right")
    t = choose("throttle", "throttle_rest", "throttle_full")
    b = choose("brake", "brake_rest", "brake_full")
    config = {"version": 1, "guid": wheel.guid, "device_name": wheel.joy.get_name(),
              "steering": {"axis": s, **{k: samples[k][s] for k in ("left", "center", "right")}},
              "throttle": {"axis": t, "rest": samples["throttle_rest"][t], "pressed": samples["throttle_full"][t]},
              "brake": {"axis": b, "rest": samples["brake_rest"][b], "pressed": samples["brake_full"][b]}}
    validate_config(config, wheel.guid, wheel.count)
    print(json.dumps(config, indent=2), flush=True)
    print("Review measured axis numbers above. Focus window and press ENTER to save; ESC cancels.", flush=True)
    while True:
        wheel.read()
        wheel.draw(["Review measured calibration in terminal.", f"Steering axis {s}; throttle axis {t}; brake axis {b}",
                    "ENTER saves; ESC cancels. Full steering range must be 900 degrees in G HUB."])
        if wheel.enter:
            destination = Path(path)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            temporary.replace(destination)
            print("Saved:", destination, flush=True)
            return config
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--device-index", type=int)
    parser.add_argument("--seconds", type=float, default=0)
    for name in ("steering", "throttle", "brake"):
        parser.add_argument(f"--{name}-axis", type=int, help="Optional measured axis override during calibration")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds must be finite and nonnegative")
    wheel = None
    try:
        wheel = Wheel(args.device_index)
        if args.calibrate:
            config = calibrate(wheel, args.config, {n: getattr(args, n + "_axis") for n in ("steering", "throttle", "brake")})
        else:
            config = load_config(args.config, wheel.guid, wheel.count) if args.config.exists() else None
        started = time.monotonic()
        next_print = 0
        while not args.seconds or time.monotonic() - started < args.seconds:
            axes, buttons, hats = wheel.read()
            values = normalize(axes, config) if config else "RAW ONLY: run --calibrate to map axes"
            lines = ["G29 - move one control at a time; ESC exits", f"Raw indexed axes: {list(enumerate(round(a, 3) for a in axes))}",
                     f"Buttons: {buttons}; hats: {hats}", str(values)]
            wheel.draw(lines)
            if time.monotonic() >= next_print:
                print(" | ".join(lines[1:]), flush=True)
                next_print = time.monotonic() + 0.2
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("Stopped.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise SystemExit(f"G29 error: {error}") from error
    finally:
        if wheel:
            wheel.close()
        else:
            import sys
            if "pygame" in sys.modules:
                sys.modules["pygame"].quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"G29 program stopped: {error}") from error
```

## g29_can_sender.py

```python
"""CODRIVE CAN encoder and verified CANable2 SLCAN transport.

Default is dry-run. --send explicitly enables Classic CAN on the CANable2 adapter.
Throttle remains an independent local value; virtual speed is explicitly supplied.
"""

import argparse
import math
from pathlib import Path
import struct
import time

from g29_test import CONFIG, Wheel, finite, load_config, normalize

BITRATE = 500_000


class CanableTransport:
    """Transport selected from actual USB descriptor and V-command response.

    A successful send means submitted to the serial adapter, NOT bus ACK or ECU RX.
    This firmware does not acknowledge individual SLCAN transmit commands.
    """
    def __init__(self, port=None):
        import can
        import serial
        from serial.tools import list_ports
        self.can = can
        self.bus = None
        self.submitted = 0
        matches = [p for p in list_ports.comports() if (p.vid, p.pid) == (0x16D0, 0x117E)
                   and (port is None or p.device.upper() == port.upper())]
        if len(matches) != 1:
            raise ValueError("Connect one CANable2 (VID 16D0 PID 117E), or select it with --port COMx. ST-LINK is not CAN.")
        self.port = matches[0].device
        # Query version without opening a CAN channel or transmitting a data frame.
        with serial.Serial(self.port, 115200, timeout=.3, write_timeout=1) as probe:
            time.sleep(.3)
            probe.reset_input_buffer()
            probe.write(b"V\r")
            probe.flush()
            reply = probe.read_until(b"\r", 256).decode("ascii", errors="replace").strip()
        if "normaldotcom/canable2" not in reply:
            raise ValueError(f"Unverified adapter firmware response: {reply!r}")
        print(f"Adapter: {self.port}; USB serial={matches[0].serial_number}; firmware={reply}", flush=True)
        self.bus = can.Bus(interface="slcan", channel=self.port, tty_baudrate=115200,
                           bitrate=BITRATE, sleep_after_open=.3, timeout=.05, ignore_config=True)
        try:
            # CANable firmware-specific documented commands; default retransmission
            # disabled to avoid accumulating old frames during a missing-ACK test.
            self.bus._write("C")
            self.bus._write("M0")
            self.bus._write("A0")
            self.bus._write("S6")
            self.bus._write("O")
        except Exception:
            self.bus.shutdown()
            self.bus = None
            raise

    def send_frames(self, frames):
        for identifier, data in frames:
            msg = self.can.Message(arbitration_id=identifier, data=data, is_extended_id=False,
                                   is_fd=False, bitrate_switch=False, check=True)
            self.bus.send(msg, timeout=.1)
            self.submitted += 1

    def close(self):
        if self.bus:
            self.bus.shutdown()
            self.bus = None


def encode_frames(speed_kph, brake_pressed, steering_deg):
    """Return (standard ID, eight data bytes); unused bytes are zero."""
    speed_kph, steering_deg = finite(speed_kph), finite(steering_deg)
    if not 0 <= speed_kph <= 655.35:
        raise ValueError("Speed must be within uint16 range: 0..655.35 km/h")
    if not -450 <= steering_deg <= 450:
        raise ValueError("Steering must be -450..450 degrees")
    if type(brake_pressed) not in (bool, int) or brake_pressed not in (0, 1):
        raise ValueError("Brake must be 0 or 1")
    return [
        (0x100, struct.pack("<H", round(speed_kph * 100)) + bytes(6)),
        (0x101, bytes([int(brake_pressed)]) + bytes(7)),
        (0x102, struct.pack("<h", round(steering_deg * 10)) + bytes(6)),
    ]


def decode_frames(frames):
    """PC-side preview of the ECU wire rules; this is not received ECU telemetry."""
    data = dict(frames)
    if set(data) != {0x100, 0x101, 0x102} or any(len(d) != 8 for d in data.values()):
        raise ValueError("Expected three standard eight-byte frames")
    if data[0x101][0] not in (0, 1):
        raise ValueError("Invalid brake byte")
    return {"speed_kph": struct.unpack("<H", data[0x100][:2])[0] * .01,
            "brake_pressed": data[0x101][0],
            "steering_deg": struct.unpack("<h", data[0x102][:2])[0] * .1}


class BrakeSwitch:
    def __init__(self, on=5.0, off=2.0):
        if not 0 <= off < on <= 100:
            raise ValueError("Brake thresholds must satisfy 0 <= off < on <= 100")
        self.on, self.off, self.pressed = on, off, False

    def update(self, percent):
        percent = finite(percent)
        if percent >= self.on:
            self.pressed = True
        elif percent <= self.off:
            self.pressed = False
        return self.pressed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Encode/display only (default)")
    mode.add_argument("--send", action="store_true", help="Send using verified CANable2 SLCAN")
    parser.add_argument("--port", help="CANable2 COM port; auto-detected by VID/PID if omitted")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--device-index", type=int)
    speed = parser.add_mutually_exclusive_group()
    speed.add_argument("--virtual-speed-kph", type=float, default=0, help="Constant test speed, independent of throttle")
    speed.add_argument("--virtual-ramp", action="store_true", help="Time-only triangular test speed: 0..60..0 km/h, 40 s period")
    parser.add_argument("--rate-hz", type=float, default=20, help="Cycles per second, three frames per cycle")
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--brake-on-pct", type=float, default=5)
    parser.add_argument("--brake-off-pct", type=float, default=2)
    parser.add_argument("--sample", nargs=3, type=float, metavar=("SPEED_KPH", "BRAKE_0_OR_1", "STEERING_DEG"),
                        help="Offline encoding check; no G29 required")
    args = parser.parse_args()
    if args.sample is not None:
        sample_frames = encode_frames(args.sample[0], args.sample[1] == 1 if args.sample[1] in (0, 1) else 2, args.sample[2])
        for identifier, data in sample_frames:
            print(f"STD 0x{identifier:03X} CLASSIC DLC=8 {data.hex(' ').upper()}")
        print("PC decode preview (not ECU RX):", decode_frames(sample_frames))
        if args.send:
            transport = CanableTransport(args.port)
            try:
                transport.send_frames(sample_frames)
                time.sleep(.1)
                print(f"Submitted {transport.submitted} frames to adapter; verify can_*_rx_count on STM32.")
            finally:
                transport.close()
        return
    if not math.isfinite(args.rate_hz) or not 0 < args.rate_hz <= 100:
        parser.error("--rate-hz must be in (0, 100]")
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds must be finite and nonnegative")
    encode_frames(args.virtual_speed_kph, False, 0)
    switch = BrakeSwitch(args.brake_on_pct, args.brake_off_pct)
    wheel = None
    transport = None
    try:
        # Validate file before opening a device; never use guessed or zeroed calibration.
        config = load_config(args.config)
        wheel = Wheel(args.device_index)
        load_config(args.config, wheel.guid, wheel.count)
        # Settle initial DirectInput state before opening CAN; keep pumping events.
        warmup_end = time.monotonic() + 2
        while time.monotonic() < warmup_end:
            wheel.read()
            wheel.draw(["Waiting for initial G29 input state (2 s); CAN not open yet."])
            time.sleep(.02)
        if args.send:
            transport = CanableTransport(args.port)
        label = "SEND: adapter submissions (not confirmed ECU RX)" if transport else "DRY RUN: no physical CAN transmission"
        print(f"{label}; standard Classic CAN DLC=8, bitrate={BITRATE}", flush=True)
        started = time.monotonic()
        deadline = started
        next_print = started
        cycles = 0
        while not args.seconds or time.monotonic() - started < args.seconds:
            axes, _, _ = wheel.read()
            values = normalize(axes, config)
            elapsed = time.monotonic() - started
            speed_kph = 60 * (1 - abs((elapsed % 40) / 20 - 1)) if args.virtual_ramp else args.virtual_speed_kph
            brake = switch.update(values["brake_pct"])
            frames = encode_frames(speed_kph, brake, values["steering_deg"])
            if transport:
                transport.send_frames(frames)
            cycles += 1
            lines = [label, f"Cycles: {cycles}; virtual speed: {speed_kph:.2f} km/h",
                     f"Steering: {values['steering_deg']:+.1f} deg; brake: {values['brake_pct']:.1f}% -> {int(brake)}",
                     f"Throttle (local only): {values['throttle_pct']:.1f}%"]
            lines += [f"0x{i:03X} DLC=8 {d.hex(' ').upper()}" for i, d in frames]
            parsed = decode_frames(frames)
            lines += [f"PC decode ONLY: speed={parsed['speed_kph']:.2f} brake={parsed['brake_pressed']} steering={parsed['steering_deg']:+.1f}",
                      "Actual ECU parsing: run g29_ecu_monitor.py on ST-LINK COM7"]
            wheel.draw(lines)
            if time.monotonic() >= next_print:
                print(" | ".join(lines[1:]), flush=True)
                next_print = time.monotonic() + 0.2
            deadline += 1 / args.rate_hz
            if deadline < time.monotonic():
                deadline = time.monotonic()
            time.sleep(max(0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped; no further input processed.")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        raise SystemExit(f"Sender stopped: {error}") from error
    finally:
        if transport:
            print(f"Adapter submissions: {transport.submitted}; physical reception requires STM32 counters.")
            transport.close()
        if wheel:
            wheel.close()
        else:
            import sys
            if "pygame" in sys.modules:
                sys.modules["pygame"].quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as error:
        # Includes python-can / pyserial disconnect errors without a stale-input retry.
        raise SystemExit(f"CAN program stopped: {error}") from error
```

## g29_ecu_monitor.py

```python
"""Display VehicleState actually reported by STM32 over ST-LINK UART.

Requires the updated firmware. Does not infer ECU reception from PC transmissions.
py -3.12 E:\\stm32\\g29_ecu_monitor.py --port COM7
"""

import argparse
import time


def parse_telemetry(line):
    if not line.startswith("ECU,"):
        raise ValueError("Not an ECU telemetry line")
    fields = dict(item.split("=", 1) for item in line.strip().split(",")[1:])
    required = ("ms", "speed_raw", "brake", "steer_raw", "rx", "valid", "speed_n", "brake_n",
                "steer_n", "speed_ms", "brake_ms", "steer_ms", "err", "invalid", "lost", "bo", "lec")
    result = {key: int(fields[key]) for key in required}
    if not 0 <= result["speed_raw"] <= 65535 or result["brake"] not in (0, 1) or not -32768 <= result["steer_raw"] <= 32767:
        raise ValueError("Invalid ECU values")
    return result


def format_telemetry(values):
    v = values
    ages = [(v["ms"] - v[key + "_ms"]) & 0xFFFFFFFF for key in ("speed", "brake", "steer")]
    fresh = all(v[k + "_n"] > 0 for k in ("speed", "brake", "steer")) and max(ages) <= 1000
    return (f"ECU {'LIVE' if fresh else 'NO CAN / STALE'} | "
            f"speed={v['speed_raw'] * .01:7.2f} km/h brake={v['brake']} "
            f"steering={v['steer_raw'] * .1:+7.1f} deg | RX={v['rx']} valid={v['valid']} "
            f"IDs={v['speed_n']}/{v['brake_n']}/{v['steer_n']} age_ms={ages} | "
            f"HALerr={v['err']} invalid={v['invalid']} lost={v['lost']} busoff={v['bo']} LEC={v['lec']}")


def main():
    import serial
    from serial.tools import list_ports
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="ST-LINK COM port, auto-detected if omitted")
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()
    ports = [p for p in list_ports.comports() if (p.vid, p.pid) == (0x0483, 0x374E)
             and (args.port is None or p.device.upper() == args.port.upper())]
    if len(ports) != 1:
        parser.error("Connect one NUCLEO ST-LINK or select --port COM7; close other serial monitors")
    print(f"STM32 actual RX/VehicleState monitor: {ports[0].device}, 115200 8N1", flush=True)
    print("Close keyboard_test.py/other COM7 monitors first. Ctrl+C exits.", flush=True)
    started = last_valid = last_warning = time.monotonic()
    buffer = bytearray()
    with serial.Serial(ports[0].device, 115200, timeout=.1) as port:
        while not args.seconds or time.monotonic() - started < args.seconds:
            buffer.extend(port.read(min(max(port.in_waiting, 1), 4096)))
            while b"\n" in buffer:
                raw, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                try:
                    values = parse_telemetry(raw.decode("ascii"))
                except (UnicodeError, ValueError, KeyError):
                    continue
                last_valid = time.monotonic()
                print(format_telemetry(values), flush=True)
            if len(buffer) > 4096:
                buffer.clear()
            now = time.monotonic()
            if now - last_valid > 2 and now - last_warning > 2:
                print("NO ECU TELEMETRY: flash updated firmware and Resume CPU; check COM port.", flush=True)
                last_warning = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as error:
        raise SystemExit(f"ECU monitor stopped: {error}") from error
```

## test_g29_can.py

```python
"""Offline contract tests: py -3.12 -m unittest -v test_g29_can (in E:\\stm32)."""

import copy
import struct
import unittest

from g29_can_sender import BrakeSwitch, encode_frames
from g29_test import normalize, validate_config
from g29_ecu_monitor import parse_telemetry, format_telemetry


class ContractTests(unittest.TestCase):
    def setUp(self):
        # Synthetic calibration fixture only, never written as device calibration.
        self.config = {"version": 1, "guid": "fixture",
                       "steering": {"axis": 2, "left": -0.9, "center": 0.1, "right": 0.95},
                       "throttle": {"axis": 0, "rest": 1.0, "pressed": -1.0},
                       "brake": {"axis": 4, "rest": -1.0, "pressed": 1.0}}

    def test_wire_examples(self):
        frames = dict(encode_frames(12.34, True, -45.6))
        self.assertEqual(frames[0x100], bytes.fromhex("D2 04 00 00 00 00 00 00"))
        self.assertEqual(frames[0x101], bytes.fromhex("01 00 00 00 00 00 00 00"))
        self.assertEqual(frames[0x102], bytes.fromhex("38 FE 00 00 00 00 00 00"))

    def test_endpoints_roundtrip(self):
        for speed in (0, 12.34, 655.35):
            for steering in (-450, -0.1, 0, 0.1, 450):
                for brake in (False, True):
                    frames = dict(encode_frames(speed, brake, steering))
                    self.assertEqual(set(frames), {0x100, 0x101, 0x102})
                    self.assertTrue(all(len(d) == 8 for d in frames.values()))
                    self.assertAlmostEqual(struct.unpack("<H", frames[0x100][:2])[0] * .01, speed)
                    self.assertAlmostEqual(struct.unpack("<h", frames[0x102][:2])[0] * .1, steering)
                    self.assertEqual(frames[0x101][0], int(brake))

    def test_invalid_signals(self):
        for args in ((-1, 0, 0), (656, 0, 0), (0, 2, 0), (0, 0, 451),
                     (float("nan"), 0, 0), (0, 0, float("inf"))):
            with self.assertRaises(ValueError):
                encode_frames(*args)

    def test_measured_axes_center_and_pedal_polarity(self):
        validate_config(self.config, "fixture", 6)
        rest = normalize([1, 0, .1, 0, -1, 0], self.config)
        self.assertEqual(rest, {"steering_deg": 0, "throttle_pct": 0, "brake_pct": 0})
        full = normalize([-1, 0, -.9, 0, 1, 0], self.config)
        self.assertEqual(full, {"steering_deg": -450, "throttle_pct": 100, "brake_pct": 100})
        self.assertEqual(normalize([0, 0, .95, 0, 0, 0], self.config)["steering_deg"], 450)

    def test_reversed_steering(self):
        config = copy.deepcopy(self.config)
        config["steering"].update(left=.95, right=-.9)
        validate_config(config)
        self.assertEqual(normalize([1, 0, -.9, 0, -1, 0], config)["steering_deg"], 450)
        self.assertEqual(normalize([1, 0, .95, 0, -1, 0], config)["steering_deg"], -450)

    def test_shared_or_flat_axes_rejected(self):
        for change in ("shared", "flat", "wrong_guid"):
            config = copy.deepcopy(self.config)
            if change == "shared":
                config["brake"]["axis"] = 0
            elif change == "flat":
                config["brake"]["pressed"] = config["brake"]["rest"]
            else:
                config["guid"] = "different"
            with self.assertRaises(ValueError):
                validate_config(config, "fixture", 6)

    def test_brake_hysteresis(self):
        switch = BrakeSwitch(5, 2)
        self.assertEqual([switch.update(v) for v in (0, 4, 5, 4, 3, 2, 0)],
                         [False, False, True, True, True, False, False])

    def test_actual_ecu_telemetry(self):
        line = ("ECU,ms=1000,speed_raw=1234,brake=1,steer_raw=-456,rx=3,valid=3,"
                "speed_n=1,brake_n=1,steer_n=1,speed_ms=900,brake_ms=901,steer_ms=902,"
                "err=0,invalid=0,lost=0,bo=0,lec=0\r\n")
        values = parse_telemetry(line)
        self.assertIn("ECU LIVE", format_telemetry(values))
        self.assertIn("12.34", format_telemetry(values))
        self.assertIn("-45.6", format_telemetry(values))
        values["ms"] = 2100
        self.assertIn("STALE", format_telemetry(values))
        values["ms"] = 1000
        values["brake_n"] = 0
        self.assertIn("STALE", format_telemetry(values))

    def test_incomplete_telemetry_rejected(self):
        with self.assertRaises((ValueError, KeyError)):
            parse_telemetry("ECU,ms=1000,speed_raw=12")
        with self.assertRaises(ValueError):
            parse_telemetry("PC decode: speed=12.34")


if __name__ == "__main__":
    unittest.main()
```

## requirements_g29.txt

```text
pygame==2.6.1
python-can==4.6.1
pyserial==3.5
```
